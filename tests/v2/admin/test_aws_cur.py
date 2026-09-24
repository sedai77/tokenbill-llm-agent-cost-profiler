"""AWS CUR 2.0 adapter (SPEC §5.13, D31; brief ADMIN acceptance 5)."""

from __future__ import annotations

import csv
import gzip
import io
from pathlib import Path

import pytest

from tokenbill.adapters.anthropic_admin import BadRecord
from tokenbill.adapters.cloud_billing import (
    PARQUET_MESSAGE,
    AwsCurAdapter,
    token_unit,
    usage_tokens,
)
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
    opts,
    p,
    read,
    result_json,
    tokens,
    verified_rules,
    write_gz,
)

CSV = "cloud/cur2_bedrock_2026-09.csv"
GZ = "cloud/cur2_bedrock_2026-09.csv.gz"
COLUMNS = ["line_item_usage_start_date", "line_item_usage_account_id", "line_item_line_item_type",
           "line_item_product_code", "line_item_usage_type", "line_item_usage_amount",
           "pricing_unit", "line_item_currency_code", "line_item_unblended_cost",
           "line_item_net_unblended_cost", "line_item_iam_principal", "tags"]
PAYMENTS_ROLE = "arn:aws:sts::111122223333:assumed-role/PaymentsAppRole/i-0abc123"


def _row(**kw: str) -> dict[str, str]:
    row = {"line_item_usage_start_date": "2026-09-01T10:00:00Z",
           "line_item_usage_account_id": "111122223333", "line_item_line_item_type": "Usage",
           "line_item_product_code": "AmazonBedrock",
           "line_item_usage_type": "USE1-MP:USE1_InputTokenCount-Units",
           "line_item_usage_amount": "2", "pricing_unit": "1K tokens",
           "line_item_currency_code": "USD", "line_item_unblended_cost": "0.011",
           "line_item_net_unblended_cost": "0.0099", "line_item_iam_principal": PAYMENTS_ROLE,
           "tags": ""}
    row.update(kw)
    return row


def _write(path: Path, rows: list[dict[str, str]], columns: list[str] = COLUMNS) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([r.get(c, "") for c in columns])
    if path.suffix == ".gz":
        return write_gz(path, buf.getvalue().encode())
    path.write_text(buf.getvalue())
    return path


def test_fixture_cost_lines_and_aggregates() -> None:
    result = read("aws-cur", fixture(CSV))
    expect = manifest_entry(CSV)["expect"]
    assert len(result.cost_lines) == expect["cost_lines"]
    assert sum(c.amount_nano for c in result.cost_lines) == expect["amount_nano"]
    assert sum(c.list_amount_nano for c in result.cost_lines) == expect["list_amount_nano"]
    assert len(result.aggregates) == expect["aggregates"]
    assert tokens(result) == expect["usage_tokens"]
    for key in ("rows_skipped_not_bedrock", "rows_skipped_tax", "rows_unit_unknown"):
        assert result.stats[key] == expect[key]
    for line in result.cost_lines:
        assert line.source_kind == "aws.cur2" and line.channel == "bedrock"
        assert line.workspace_id in (h("111122223333"), h("444455556666"))
        assert line.sku and line.description                 # provider line description
        assert CANARY not in line.description
        assert line.model is None and line.token_type is None  # every rule unverified
    for agg in result.aggregates:
        assert agg.source_kind == "aws.cur2"
        d = dims(agg)
        assert d["channel"] == "bedrock" and d["sku"]
        assert agg.usage.total_input == agg.usage.uncached_input      # unmapped → uncached
    (note,) = [n for n in result.notes if n.code == "dq.unmapped_sku" and n.tokens]
    assert note.tokens == expect["usage_tokens"]
    assert result.capabilities == frozenset({"aggregates", "cost", "attribution.team"})


def test_gz_equals_csv() -> None:
    a = read("aws-cur", fixture(CSV))
    b = read("aws-cur", fixture(GZ))
    assert [to_json(x) for x in a.cost_lines] == [to_json(x) for x in b.cost_lines]
    assert [to_json(x) for x in a.aggregates] == [to_json(x) for x in b.aggregates]


def test_net_versus_unblended_and_credit_lines() -> None:
    result = read("aws-cur", fixture(CSV))
    usage = [c for c in result.cost_lines if c.cost_type is None]      # usage line items
    assert all(c.amount_nano <= c.list_amount_nano for c in usage)
    assert sum(c.amount_nano for c in usage) < sum(c.list_amount_nano for c in usage)
    discounted = [c for c in usage if c.sku.startswith("USE1-MP:")]
    assert all(c.amount_nano * 10 == c.list_amount_nano * 9 for c in discounted)  # net = 0.9 ×
    credits = [c for c in result.cost_lines if c.cost_type == "Credit"]
    assert [c.amount_nano for c in credits] == [-1_250_000_000, -1_250_000_000]
    assert all(c.principal is None for c in credits)


def test_principals_become_p_and_teams() -> None:
    result = read("aws-cur", fixture(CSV))
    principals = {c.principal for c in result.cost_lines if c.principal}
    assert p(PAYMENTS_ROLE) in principals
    assert all(x.startswith("p_") for x in principals)
    assert result.source.principal_key_id is not None
    teams = {dims(a).get("team") for a in result.aggregates}
    assert {"payments", "search", "ml-research"} <= teams     # role map, tag
    assert None in teams                                       # marketplace row: no principal
    raw = [PAYMENTS_ROLE, "PaymentsAppRole", "i-0abc123", "svc-legacy", "DataSciRole"]
    assert_person_free(result, *raw)
    assert CANARY not in result_json(result)


def test_central_mode_without_principal_key(tmp_path: Path) -> None:
    result = AwsCurAdapter().read(fixture(CSV), opts(identity_mode="central",
                                                     principal_key=None, principal_key_id=None))
    assert all(c.principal is None for c in result.cost_lines)
    assert result.source.principal_key_id is None
    assert {dims(a).get("team") for a in result.aggregates} >= {"payments"}


def test_verified_rules_map_buckets_and_units(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(catalog, "SKU_RULES", verified_rules())
    rows = [
        _row(line_item_usage_type="USE1-MP:USE1_InputTokenCount-Units",
             line_item_usage_amount="1234.567", pricing_unit="1K tokens"),
        _row(line_item_usage_type="USE1-MP:USE1_OutputTokenCount-Units",
             line_item_usage_amount="0.098765", pricing_unit="1M tokens"),
        _row(line_item_usage_type="USE1-MP:USE1_CacheReadInputTokenCount_Global-Units",
             line_item_usage_amount="5.5", pricing_unit="1M tokens"),
        _row(line_item_usage_type="USE1-MP:USE1_CacheWriteInputTokenCount-Units",
             line_item_usage_amount="250", pricing_unit="1K tokens"),
        _row(line_item_usage_type="USE1-MP:USE1_CacheWrite1hInputTokenCount-Units",
             line_item_usage_amount="3", pricing_unit="Units"),        # rule unit: 1M
        _row(line_item_usage_type="USE1-MP:USE1_InputTokenCount_Global_Batch-Units",
             line_item_usage_amount="7", pricing_unit="1K tokens"),
    ]
    result = read("aws-cur", _write(tmp_path / "cur.csv", rows))
    by = {(dims(a)["endpoint_scope"], dims(a)["service_tier"]): a.usage for a in result.aggregates}
    regional = by[("regional", "standard")]
    assert regional.uncached_input == 1_234_567
    assert regional.output == 98_765
    assert regional.cache_write_5m == 250_000
    assert regional.cache_write_1h == 3_000_000
    assert by[("global", "standard")].cache_read == 5_500_000
    assert by[("global", "batch")].uncached_input == 7_000
    assert not [n for n in result.notes if n.code == "dq.unmapped_sku"]
    assert all("sku" not in dims(a) for a in result.aggregates)
    tt = {c.sku: c.token_type for c in result.cost_lines}
    assert tt["USE1-MP:USE1_OutputTokenCount-Units"] == "output"
    assert {c.endpoint_scope for c in result.cost_lines} == {"regional", "global"}
    assert result.stats.get("unit_rule_conflicts", 0) == 3   # "1K tokens" rows vs 1M rule unit


def test_descriptions_and_line_item_types(tmp_path: Path) -> None:
    rows = [_row(line_item_line_item_description="Claude Opus 5 input tokens"),
            _row(line_item_line_item_type="Credit", line_item_net_unblended_cost="-1",
                 line_item_unblended_cost="-1", line_item_usage_amount="0",
                 line_item_line_item_description=""),
            _row(line_item_line_item_type="DiscountedUsage",
                 line_item_usage_start_date="2026-09-02T00:00:00Z")]
    cols = [*COLUMNS, "line_item_line_item_description"]
    result = read("aws-cur", _write(tmp_path / "cur.csv", rows, cols))
    got = sorted((c.date_utc, c.cost_type or "", c.description) for c in result.cost_lines)
    assert got == [("2026-09-01", "", "Claude Opus 5 input tokens"),
                   ("2026-09-01", "Credit", "USE1-MP:USE1_InputTokenCount-Units"),
                   ("2026-09-02", "", "USE1-MP:USE1_InputTokenCount-Units")]
    assert len(result.aggregates) == 2                      # credit rows carry no tokens


def test_only_bedrock_rows_and_anthropic_usage_types(tmp_path: Path) -> None:
    rows = [_row(), _row(line_item_product_code="AmazonEC2",
                         line_item_usage_type="USE1-BoxUsage:m7i.large"),
            _row(line_item_product_code="AWSMarketplace",
                 line_item_usage_type="USE1-anthropic.claude-opus-5-output-tokens"),
            _row(line_item_line_item_type="Tax")]
    result = read("aws-cur", _write(tmp_path / "cur.csv", rows))
    assert {c.sku for c in result.cost_lines} == {"USE1-MP:USE1_InputTokenCount-Units",
                                                  "USE1-anthropic.claude-opus-5-output-tokens"}
    assert result.stats["rows_skipped_not_bedrock"] == 1
    assert result.stats["rows_skipped_tax"] == 1


def test_hourly_rows_sum_to_day(tmp_path: Path) -> None:
    rows = [_row(line_item_usage_start_date=f"2026-09-01T{h:02d}:00:00Z") for h in range(24)]
    result = read("aws-cur", _write(tmp_path / "cur.csv", rows))
    (line,) = result.cost_lines
    assert line.amount_nano == 24 * 9_900_000 and line.list_amount_nano == 24 * 11_000_000
    (agg,) = result.aggregates
    assert agg.usage.uncached_input == 48_000
    assert agg.bucket_end_ms - agg.bucket_start_ms == 86_400_000


def test_parquet_is_refused_with_csv_hint(tmp_path: Path) -> None:
    path = tmp_path / "cur2-00001.snappy.parquet"
    path.write_bytes(b"PAR1" + b"\0" * 64 + b"PAR1")
    adapter = AwsCurAdapter()
    assert adapter.sniff(path, path.read_bytes()[:64])
    with pytest.raises(SourceError, match="CSV") as err:
        adapter.read(path, opts())
    assert PARQUET_MESSAGE in str(err.value)
    odd = tmp_path / "export.bin"
    odd.write_bytes(b"PAR1xxxx")
    with pytest.raises(SourceError, match="CSV"):
        adapter.read(odd, opts())


def test_legacy_cur_columns(tmp_path: Path) -> None:
    legacy = ["lineItem/UsageStartDate", "lineItem/UsageAccountId", "lineItem/LineItemType",
              "lineItem/ProductCode", "lineItem/UsageType", "lineItem/UsageAmount",
              "pricing/unit", "lineItem/CurrencyCode", "lineItem/UnblendedCost",
              "lineItem/NetUnblendedCost", "lineItem/IamPrincipal", "tags"]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(legacy)
    r = _row()
    w.writerow([r[c] for c in COLUMNS])
    path = tmp_path / "legacy.csv"
    path.write_text(buf.getvalue())
    assert AwsCurAdapter().sniff(path, path.read_bytes())
    result = read("aws-cur", path)
    assert len(result.cost_lines) == 1 and len(result.aggregates) == 1


def test_bad_rows_quarantined_and_strict_mode(tmp_path: Path) -> None:
    rows = [_row(), _row(line_item_usage_account_id=""),
            _row(line_item_currency_code="EUR"),
            _row(line_item_unblended_cost="", line_item_net_unblended_cost=""),
            _row(line_item_usage_start_date="Sept 1"),
            _row(line_item_usage_amount="-4"),
            _row(line_item_net_unblended_cost="1e40"),
            _row(line_item_usage_type="")]
    path = _write(tmp_path / "cur.csv", rows)
    result = read("aws-cur", path)
    assert [q.reason for q in result.quarantined] == [
        "missing:line_item_usage_account_id", "bad_type:currency",
        "missing:line_item_unblended_cost", "bad_type:line_item_usage_start_date",
        "bad_usage", "bad_type:line_item_net_unblended_cost", "missing:line_item_usage_type"]
    assert [q.locator for q in result.quarantined][0] == "line:3"
    (line,) = result.cost_lines                    # a quarantined row adds nothing
    assert line.amount_nano == 9_900_000 and len(result.aggregates) == 1
    with pytest.raises(SourceError, match="line:3"):
        read("aws-cur", path, lenient=False)


def test_ragged_and_blank_csv_lines(tmp_path: Path) -> None:
    path = tmp_path / "cur.csv"
    good = _write(tmp_path / "g.csv", [_row()]).read_text()
    path.write_text(good + "only,three,cells\n\n,,,,,,,,,,,\n")
    result = read("aws-cur", path)
    assert len(result.cost_lines) == 1
    assert [q.reason for q in result.quarantined] == ["bad_csv"]


def test_directory_of_export_parts(tmp_path: Path) -> None:
    d = tmp_path / "cur" / "data" / "BILLING_PERIOD=2026-09"
    _write(d / "part-00001.csv.gz", [_row()])
    _write(d / "part-00002.csv.gz", [_row(line_item_usage_start_date="2026-09-01T11:00:00Z")])
    result = read("aws-cur", tmp_path / "cur")
    (line,) = result.cost_lines
    assert line.amount_nano == 2 * 9_900_000
    assert result.stats["files"] == 2


def test_corrupt_gzip_is_a_source_error(tmp_path: Path) -> None:
    path = tmp_path / "cur.csv.gz"
    data = gzip.compress(fixture(CSV).read_bytes())
    path.write_bytes(data[: len(data) // 2])
    with pytest.raises(SourceError):
        read("aws-cur", path)


def test_token_unit_parser() -> None:
    assert token_unit("1K tokens") == 1000
    assert token_unit("1M Tokens") == 1_000_000
    assert token_unit("Million tokens") == 1_000_000
    assert token_unit("Thousand Input Tokens") == 1000
    assert token_unit("1,000 tokens") == 1000
    assert token_unit("tokens") == 1
    assert token_unit("1000000 tokens") == 1_000_000
    for bad in ("Units", "count", "", None, 5, "0 tokens", "1G tokens", "x" * 80):
        assert token_unit(bad) is None


def test_usage_tokens_exact_and_rounding() -> None:
    assert usage_tokens("1234.567", 1000) == (1_234_567, False)
    assert usage_tokens("0.0000015", 1_000_000) == (2, True)     # 1.5 → 2 (half-even)
    assert usage_tokens("0.0000025", 1_000_000) == (2, True)     # 2.5 → 2
    with pytest.raises(BadRecord):
        usage_tokens("-1", 1)
    with pytest.raises(BadRecord):
        usage_tokens("1e20", 1_000_000)


def test_oversize_csv_field_is_quarantined(tmp_path: Path) -> None:
    rows = [_row(), _row(tags="x" * 200_000), _row(line_item_usage_start_date=
                                                   "2026-09-02T00:00:00Z")]
    result = read("aws-cur", _write(tmp_path / "cur.csv", rows))
    assert [q.reason for q in result.quarantined] == ["bad_csv"]
    assert len(result.cost_lines) == 2


def test_team_tags_and_role_fallbacks(tmp_path: Path) -> None:
    rows = [
        _row(line_item_iam_principal="arn:aws:iam::111122223333:user/alice",
             tags='{"iamPrincipal/team": "data"}'),
        _row(line_item_iam_principal="arn:aws:iam::111122223333:user/bob", tags="not json",
             line_item_usage_start_date="2026-09-02T00:00:00Z"),
        _row(line_item_iam_principal="arn:aws:iam::111122223333:user/carol", tags="[1]",
             line_item_usage_start_date="2026-09-03T00:00:00Z"),
        _row(line_item_iam_principal="arn:aws:iam::111122223333:user/dan",
             tags='{"iamPrincipal/team": "bad\\u0000value"}',
             line_item_usage_start_date="2026-09-04T00:00:00Z"),
    ]
    result = read("aws-cur", _write(tmp_path / "cur.csv", rows))
    teams = [dims(a)["team"] for a in sorted(result.aggregates,
                                            key=lambda a: a.bucket_start_ms)]
    assert teams == ["data", "(unmapped)", "(unmapped)", "(unmapped)"]
    assert_person_free(result, "alice", "bob", "carol", "dan")


def test_rules_with_non_token_buckets_are_ignored(tmp_path: Path, monkeypatch) -> None:
    rule = catalog.SkuRule(source_kind="aws.cur2", pattern=r".*InputTokenCount-Units",
                           model=None, bucket="web_search_requests", endpoint_scope=None,
                           service_tier=None, unit_tokens=1000, verified=True, source="t")
    monkeypatch.setattr(catalog, "SKU_RULES", (rule,))
    result = read("aws-cur", _write(tmp_path / "cur.csv", [_row(line_item_usage_amount="0.0015",
                                                                pricing_unit="1K tokens")]))
    (agg,) = result.aggregates
    assert dims(agg)["sku"] and agg.usage.uncached_input == 2      # 1.5 → 2, unmapped path
    assert result.stats["token_amounts_rounded"] == 1

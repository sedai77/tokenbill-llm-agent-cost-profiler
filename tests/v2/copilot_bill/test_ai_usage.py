"""github-ai-usage: GitHub's test row, the 40-row acceptance fixture, legacy / April / preview
files, quarantine and the date parser (addendum §5.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tokenbill.adapters.github_billing import AiUsageReportAdapter, parse_report_date
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.errors import PrivacyError, SourceError, UsageError
from tokenbill.core.ids import key_id, pseudonym

from .helpers import (
    ENTRIES,
    FIXTURES,
    NAME_KEY,
    PRINCIPAL_KEY,
    coverage_aggs,
    dump,
    ms,
    notes,
    opts,
    read,
    token_aggs,
    write,
)

MAIN = "ai_usage_2026-09.csv"
HEADER = ("date,product,sku,quantity,unit_type,applied_cost_per_quantity,gross_amount,"
          "discount_amount,net_amount,username,organization,repository,cost_center_name,model,"
          "input,output,cache_read,cache_write\n")


def _p(login: str) -> str:
    return pseudonym(PRINCIPAL_KEY, "p", login)


def test_github_test_row() -> None:
    r = read("ai_usage_github_test_row.csv")
    (line,) = r.cost_lines
    assert (line.list_amount_nano, line.amount_nano) == (427_262_130, 0)
    assert line.list_amount_nano - line.amount_nano == 427_262_130          # discount
    assert (line.model, line.routing, line.speed, line.pseudo) == (
        "claude-haiku-4-5", "auto", "standard", None)
    assert line.cost_type == "ai_credit.user" and line.principal == _p("mona")
    assert (line.quantity, line.unit, line.sku, line.workspace_id) == (
        "42.726213", "ai-credits", "copilot_ai_credit", "example-org")
    assert r.stats["rounding_remainder_e18"] == 0                          # net is exactly 0
    assert r.stats["rounding_remainder_e18.gross"] == 100                  # 1E-16 USD tail
    assert not token_aggs(r) and r.stats["rows_without_tokens"] == 1       # no token columns
    assert notes(r) == {"dq.copilot_report_quota_ignored": 1}


def test_forty_row_fixture() -> None:
    r = read(MAIN)
    exp = ENTRIES[MAIN]["expect"]
    lines = r.cost_lines
    assert len(lines) == exp["cost_lines"] == 40 and not r.quarantined
    assert len(token_aggs(r)) == exp["token_aggregates"]
    assert len(coverage_aggs(r)) == exp["coverage_aggregates"]
    assert sum(c.amount_nano for c in lines) == exp["net_nano"]
    assert sum(c.list_amount_nano for c in lines) == exp["gross_nano"]
    assert r.stats["rounding_remainder_e18"] == exp["rounding_remainder_e18"] == 100
    # aggregates sum to the rows: tokens, net and gross; coverage per day sums to the rows
    aggs = token_aggs(r)
    assert sum(a.usage.total_input + a.usage.output for a in aggs) == exp["tokens"]
    assert sum(a.reported_cost_nano for a in aggs) == exp["net_nano"]
    assert sum(a.list_cost_nano for a in aggs) == exp["gross_nano"]
    assert sum(a.reported_cost_nano for a in coverage_aggs(r)) == exp["net_nano"]
    assert sum(v for k, v in r.stats.items() if k.startswith("rows:")) == 40
    assert "dq.copilot_row_identity" not in notes(r)
    assert r.capabilities == frozenset({"aggregates", "cost", "copilot_billing"})
    assert not (r.requests or r.sessions or r.events or r.config)            # R-E43: no inferences
    assert all(c.fetched_ms == ms("2026-09-23") for c in lines)
    assert all(a.fetched_ms == ms("2026-09-23") for a in r.aggregates)


def test_direct_rows_and_pseudo_code_review() -> None:
    r = read(MAIN)
    direct = [c for c in r.cost_lines if c.principal is None]
    assert len(direct) == 5 == notes(r)["dq.copilot_unattributed_rows"]
    for c in direct:
        assert (c.cost_type, c.pseudo, c.workload, c.model, c.team) == (
            "ai_credit.direct", "code_review", "copilot_code_review", None, None)
        assert c.amount_nano == c.list_amount_nano                         # metered, no pool
    review = [a for a in token_aggs(r) if dict(a.dims).get("pseudo") == "code_review"]
    assert len(review) == 5 and all(a.usage.uncached_input == 40_000 for a in review)
    assert all("model" not in dict(a.dims) for a in review)
    agent = [c for c in r.cost_lines if c.pseudo == "cloud_agent"]
    assert [(c.workload, c.principal) for c in agent] == [("copilot_cloud_agent", _p("octo-alice"))]
    sku_agent = [c for c in r.cost_lines if c.sku == "coding_agent_ai_credit"]
    assert len(sku_agent) == 2 and {c.workload for c in sku_agent} == {"copilot_cloud_agent"}
    spark = [c for c in r.cost_lines if c.sku == "spark_ai_credit"]
    assert [c.cost_type for c in spark] == ["ai_credit.user"]


def test_people_are_pseudonymized_and_mapped() -> None:
    r = read(MAIN)
    text = dump(r)
    assert CANARY_LOGIN not in text and "octo-" not in text and CANARY_LOGIN not in repr(r)
    assert r.source.principal_key_id == key_id(PRINCIPAL_KEY)
    assert r.source.name_key_id == key_id(NAME_KEY)
    by_p = {c.principal: c.team for c in r.cost_lines if c.principal}
    assert by_p[_p("octo-alice")] == "platform" and by_p[_p(CANARY_LOGIN)] == "infra"
    assert by_p[_p("octo-dave")] is None                                   # unmapped
    repo = {c.repo for c in r.cost_lines if c.repo}
    assert repo == {pseudonym(NAME_KEY, "h", "acme-a/web-app"),
                    pseudonym(NAME_KEY, "h", "acme-a/payments-api")}
    assert "web-app" not in text and "payments-api" not in text
    cc = [c for c in r.cost_lines if c.cost_center]
    assert [c.cost_center for c in cc] == ["Platform, EMEA"]              # quoted CSV field


def test_models_dates_and_finality() -> None:
    r = read(MAIN)
    dates = {c.date_utc for c in r.cost_lines}
    assert {"2026-09-05", "2026-09-06", "2026-09-10", "2026-08-25"} <= dates   # M/D/YY, ISO ts
    fast = [c for c in r.cost_lines if c.speed == "fast"]
    assert {(c.model, c.date_utc) for c in fast} == {("claude-opus-4-8", "2026-09-10"),
                                                     ("claude-opus-4-8", "2026-09-11")}
    auto = [c for c in r.cost_lines if c.routing == "auto"]
    assert len(auto) == 3 and {c.model for c in auto} == {"claude-haiku-4-5"}
    opus = [c for c in r.cost_lines if c.model == "claude-opus-5-5"]
    assert len(opus) == 7 and min(c.date_utc for c in opus) == "2026-09-22"
    # an Auto row and a direct row of one model on one day are two lines (not a duplicate)
    assert len([c for c in r.cost_lines if c.date_utc == "2026-09-09"]) == 2
    assert "dq.copilot_duplicate_key_summed" not in notes(r)
    for c in r.cost_lines:
        assert c.finality == ("final" if c.date_utc < "2026-09-01" else "provisional")
    assert {a.finality for a in coverage_aggs(r) if a.bucket_start_ms < ms("2026-09-01")} == {
        "final"}
    # September closes on 2026-10-01; its last days are final once 3 days (report lag) passed
    assert {c.finality for c in read(MAIN, now="2026-10-02").cost_lines} == {"final"}
    at_close = read(MAIN, now="2026-09-25").cost_lines
    assert {c.finality for c in at_close if c.date_utc >= "2026-09-01"} == {"provisional"}


def test_token_columns_under_excl() -> None:
    r = read(MAIN)
    agg = next(a for a in token_aggs(r) if dict(a.dims).get("model") == "claude-sonnet-5"
               and a.bucket_start_ms == ms("2026-08-25"))
    u = agg.usage
    assert (u.uncached_input, u.output, u.cache_read, u.cache_write_unknown) == (
        12_000, 3_000, 180_000, 6_000)
    assert dict(agg.dims) == {"channel": "github_copilot", "organization": "acme-a",
                              "team": "platform", "model": "claude-sonnet-5",
                              "sku": "copilot_ai_credit", "routing": "direct",
                              "speed": "standard"}
    assert agg.reported_cost_basis == "invoice" and agg.bucket_end_ms - agg.bucket_start_ms == (
        86_400_000)


def test_legacy_pru_file() -> None:
    r = read("ai_usage_legacy_pru.csv")
    assert len(r.cost_lines) == 5 and not token_aggs(r)
    assert {c.cost_type for c in r.cost_lines} == {"ai_credit.legacy_pru"}
    assert {c.unit for c in r.cost_lines} == {"requests"}
    assert notes(r)["dq.copilot_legacy_pru"] == 5
    assert notes(r)["dq.copilot_unattributed_rows"] == 1
    assert "dq.copilot_report_quota_ignored" not in notes(r)       # PRU quotas are no evidence
    assert sum(c.amount_nano for c in r.cost_lines) == ENTRIES[
        "ai_usage_legacy_pru.csv"]["expect"]["net_nano"]


def test_april_directional_and_backfill() -> None:
    r = read("ai_usage_april_preview.csv", now="2027-01-01")
    assert len(r.cost_lines) == 3 and r.stats["backfill_duplicates_dropped"] == 1
    assert {c.finality for c in r.cost_lines} == {"provisional"}             # forever
    assert notes(r)["dq.copilot_directional_report"] == 3
    assert {a.finality for a in coverage_aggs(r)} == {"provisional"}


def test_preview_columns_after_june() -> None:
    r = read("ai_usage_preview_columns.csv")
    assert notes(r)["dq.copilot_preview_columns"] == 1
    assert {c.date_utc for c in r.cost_lines} == {"2026-06-01"}
    assert sum(c.amount_nano for c in r.cost_lines) == 2 * 969_990_345


def test_edge_rows_quarantine() -> None:
    r = read("ai_usage_edge.csv")
    assert sorted(q.reason for q in r.quarantined) == sorted([
        "bad_type:date", "missing:net_amount", "bad_type:gross_amount", "bad_usage",
        "bad_usage", "missing:sku", "bad_type:organization", "missing:date",
        "bad_type:quantity"])
    assert all(q.locator.startswith("line:") for q in r.quarantined)
    assert len(r.cost_lines) == 5 and r.stats["rows"] == 14
    other = [c for c in r.cost_lines if c.cost_type == "other"]
    assert [c.sku for c in other] == ["copilot_mystery_credit"]
    assert notes(r)["dq.unmapped_sku"] == 1 and notes(r)["dq.copilot_row_identity"] == 1
    assert "2026-08-31" in {c.date_utc for c in r.cost_lines}                # +02:00 offset
    assert any(c.model is None and c.date_utc == "2026-09-04" for c in r.cost_lines)
    with pytest.raises(SourceError):
        read("ai_usage_edge.csv", lenient=False)


def test_bad_headers_and_options(tmp_path: Path) -> None:
    ad = AiUsageReportAdapter()
    wrong = write(tmp_path, "wrong.csv", "date,model,unit_type\n2026-09-01,x,y\n")
    r = ad.read(wrong, opts())
    assert [q.reason for q in r.quarantined] == ["missing:username"] and not r.cost_lines
    empty = write(tmp_path, "empty.csv", "")
    assert [q.reason for q in ad.read(empty, opts()).quarantined] == ["missing:header"]
    with pytest.raises(SourceError):
        ad.read(wrong, opts(lenient=False))
    with pytest.raises(UsageError):
        ad.read(FIXTURES / MAIN, opts(name_key=b"", name_key_id=""))
    with pytest.raises(UsageError):
        ad.read(tmp_path, opts())
    with pytest.raises(PrivacyError):
        ad.read(FIXTURES / MAIN, opts(principal_key=None, principal_key_id=None))
    direct_only = write(tmp_path, "direct.csv", HEADER + "2026-09-01,copilot,copilot_ai_credit,1,"
                        "ai-credits,0.01,0.01,0,0.01,,acme-a,,,Copilot Code Review,1,1,1,0\n")
    r2 = ad.read(direct_only, opts(principal_key=None, principal_key_id=None))
    assert r2.source.principal_key_id is None and r2.cost_lines[0].principal is None
    many = write(tmp_path, "many.csv", HEADER + "2026-09-01,a,b,c,d,e,f,g,h,i,j,k,l,m,1,2,3,4,5\n"
                 + "2026-09-01,copilot,copilot_ai_credit,1\n")
    assert sorted(q.reason for q in ad.read(many, opts()).quarantined) == [
        "bad_type:row", "missing:gross_amount"]


def test_window_and_overflow(tmp_path: Path) -> None:
    r = read(MAIN, since_ms=ms("2026-09-22"), until_ms=ms("2026-09-23"))
    assert {c.date_utc for c in r.cost_lines} == {"2026-09-22"}
    assert r.stats["rows_outside_window"] == 35
    big = str(2**53)
    path = write(tmp_path, "big.csv", HEADER + "".join(
        f"2026-09-01,copilot,copilot_ai_credit,1,ai-credits,0.01,0.01,0,0.01,u{i},acme-a,,,"
        f"GPT-5.5,{big},0,0,0\n" for i in range(2)))
    r2 = AiUsageReportAdapter().read(path, opts(team_map={"u0": "t", "u1": "t"}))
    assert [q.reason for q in r2.quarantined] == ["bad_usage"] and len(r2.cost_lines) == 1


@pytest.mark.parametrize(("text", "expected"), [
    ("2026-09-01", "2026-09-01"), ("9/1/26", "2026-09-01"), ("09/01/2026", "2026-09-01"),
    ("2026-09-01T23:30:00Z", "2026-09-01"), ("2026-09-01T23:30:00-02:00", "2026-09-02"),
    ("2026-09-01 01:00:00+0530", "2026-08-31"), ("2026-09-01T00:00", "2026-09-01"),
    ("2026-09-01T12:00:00.123456Z", "2026-09-01"), (" 2026-9-1 ", "2026-09-01"),
    ("2026-02-30", None), ("13/1/26", None), ("1969-12-31", None), ("yesterday", None),
    ("", None), ("2026-09-01T25:00:00Z", None)])
def test_parse_report_date(text: str, expected: str | None) -> None:
    assert parse_report_date(text) == expected


def test_parse_report_date_rejects_non_strings() -> None:
    assert parse_report_date(20260901) is None  # type: ignore[arg-type]

"""FOCUS 1.4 export (SPEC §14.4, D19, D26, A-8)."""

from __future__ import annotations

import csv
import dataclasses
import io
import json
import re
from collections import defaultdict
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core.errors import ContractViolation, GateFailed, TokenbillError, UsageError
from tokenbill.core.facts import load
from tokenbill.core.kanon import other_label
from tokenbill.core.labels import Basis
from tokenbill.core.types import FocusRow, LedgerCostRow
from tokenbill.finops.allocation import AllocatedCostRow
from tokenbill.outputs import focus as F

from .sample import findings, recon

SHA = "cd" * 32
RECONCILED = frozenset({"anthropic_api", "bedrock"})
X_RE = re.compile(r"^x_[A-Z][A-Za-z0-9]{1,48}$")


def row(team: str | None = "payments", users: int = 8, nano: int = 5_000_000_000, *,
        bucket: str = "output", basis: Basis = Basis.LIST, channel: str = "anthropic_api",
        date: str = "2026-09-01", path: str = "api_key", qty: int = 12_345, low: int = 0,
        high: int = 0, lane: str = "main", **kw: object) -> LedgerCostRow:
    fields = dict(date_utc=date, provider="anthropic", channel=channel, model="claude-opus-5-5",
                  bucket=bucket, team=team, cost_center="cc-1", project=None, workspace_id="h_ws",
                  lane_kind=lane, workload_class="interactive", agent_product="claude_code",
                  billing_path=path, quantity=qty, priced_nano=nano, estimated_low_nano=low,
                  estimated_high_nano=high, basis=basis,
                  rate_row_id="anthropic/anthropic_api/claude-opus-5-5/2026-09-22", n_users=users)
    fields.update(kw)
    return LedgerCostRow(**fields)  # type: ignore[arg-type]


def export(rows, **kw) -> tuple[int, list[dict[str, str]], list[str]]:
    out = io.StringIO()
    args = dict(reconciled_channels=RECONCILED, allow_unreconciled=False, rate_card_sha=SHA)
    args.update(kw)
    n = F.write_focus(rows, out, **args)  # type: ignore[arg-type]
    reader = csv.DictReader(io.StringIO(out.getvalue()))
    data = list(reader)
    assert n == len(data)
    return n, data, list(reader.fieldnames or [])


def usd(text: str) -> int:
    return int(Decimal(text) * 10**9)


def test_required_columns_and_x_names() -> None:
    _n, _rows, header = export([row()])
    mandatory = load().focus.mandatory
    assert set(mandatory) <= set(header)
    assert "ProviderName" not in header and "PublisherName" not in header
    for name in header:
        if name.startswith("x_"):
            assert X_RE.match(name), name
        else:
            load().focus.column(name)      # a FOCUS 1.4 column (KeyError otherwise)
    assert F.focus_columns() == F.FOCUS_COLUMNS
    assert all(X_RE.match(x) for x in F.X_COLUMNS)


def test_values_of_a_billed_row() -> None:
    _n, rows, _h = export([row(qty=2_000_000, nano=usd("10"), low=usd("1"), high=usd("2"))])
    r = rows[0]
    assert r["BillingCurrency"] == "USD" and r["ChargeCategory"] == "Usage"
    assert r["ChargePeriodStart"] == "2026-09-01T00:00:00Z"
    assert r["ChargePeriodEnd"] == "2026-09-02T00:00:00Z"
    assert r["BillingPeriodStart"] == "2026-09-01T00:00:00Z"
    assert r["BillingPeriodEnd"] == "2026-10-01T00:00:00Z"
    assert r["ServiceCategory"] == "AI and Machine Learning"
    assert r["PricingQuantity"] == "2" and r["PricingUnit"] == "1M Tokens"
    assert r["ConsumedQuantity"] == "2000000" and r["ConsumedUnit"] == "Tokens"
    assert r["ListCost"] == r["BilledCost"] == r["EffectiveCost"] == r["ContractedCost"] == "10"
    assert r["ListUnitPrice"] == "5"
    assert r["x_EstimatedCostLow"] == "1" and r["x_EstimatedCostHigh"] == "2"   # never in costs
    assert r["x_PriceBasis"] == "list" and r["x_Reconciled"] == "true"
    assert r["x_Source"] == "tokenbill" and r["x_Role"] == "primary"
    assert r["x_RateCardSha256"] == SHA
    assert r["SkuId"].endswith("#output")
    assert json.loads(r["Tags"]) == {"team": "payments", "cost_center": "cc-1",
                                     "workload_class": "interactive",
                                     "agent_product": "claude_code", "billing_path": "api_key"}
    assert "." not in r["ListCost"] or Decimal(r["ListCost"]) == Decimal("10")


def test_totals_equal_input_sums_per_day_and_team() -> None:
    rows = [row(team=t, users=u, nano=n, date=d, bucket=b)
            for d in ("2026-09-01", "2026-09-02")
            for t, u, n in (("payments", 9, 7_111_000_001), ("search", 6, 3_000_000_003))
            for b in ("output", "cache_read")]
    rows += [row(team="payments", nano=900, date="2026-09-02", bucket="output", lane="subagent")]
    _n, out, _h = export(rows)
    want: dict[tuple, int] = defaultdict(int)
    for r in rows:
        want[(r.date_utc, r.team)] += r.priced_nano
    got: dict[tuple, int] = defaultdict(int)
    for r in out:
        team = json.loads(r["Tags"])["team"]
        got[(r["ChargePeriodStart"][:10], team)] += usd(r["ListCost"])
        assert r["ListCost"] == r["BilledCost"]
    assert got == want


def test_unreconciled_channel_is_refused_naming_it() -> None:
    rows = [row(), row(channel="vertex"), row(channel="openai_api")]
    with pytest.raises(GateFailed) as err:
        export(rows)
    assert "openai_api, vertex" in str(err.value)
    _n, out, _h = export(rows, channels=frozenset({"anthropic_api"}))
    assert {r["x_Channel"] for r in out} == {"anthropic_api"}
    _n, out, _h = export(rows, allow_unreconciled=True)
    flags = {r["x_Channel"]: r["x_Reconciled"] for r in out}
    assert flags == {"anthropic_api": "true", "vertex": "false", "openai_api": "false"}
    vertex = next(r for r in out if r["x_Channel"] == "vertex")
    assert vertex["BilledCost"] == vertex["ListCost"] == "5"
    assert vertex["InvoiceIssuerName"] == "Google" and vertex["HostProviderName"] == "Google Cloud"


def test_allowance_rows_are_never_billed() -> None:
    rows = [row(basis=Basis.LIST_EQUIVALENT, path="subscription", channel="foundry"),
            row(basis=Basis.LIST_EQUIVALENT, path="copilot_pool", channel="github_copilot")]
    _n, out, _h = export(rows)      # list-equivalent-only channels never block the export
    for r in out:
        assert r["x_PriceBasis"] == "list_equivalent"
        assert r["ListCost"] == "5"
        assert r["BilledCost"] == r["EffectiveCost"] == r["ContractedCost"] == "0"
        assert r["ContractedUnitPrice"] == ""
        assert r["x_Reconciled"] == "false"


def test_role_enrichment_zeroes_billed_and_effective() -> None:
    _n, out, _h = export([row(), row(team="search", users=5)], role="enrichment")
    for r in out:
        assert r["BilledCost"] == r["EffectiveCost"] == "0"
        assert r["ListCost"] == r["ContractedCost"] == "5"
        assert r["x_Role"] == "enrichment"
    with pytest.raises(UsageError):
        export([row()], role="secondary")


def test_chargeback_coverage_gate() -> None:
    with pytest.raises(GateFailed):
        export([row()], chargeback=True, allocation_coverage="0.9499")
    with pytest.raises(GateFailed):
        export([row()], chargeback=True)
    with pytest.raises(UsageError):
        export([row()], chargeback=True, allocation_coverage="lots")
    with pytest.raises(UsageError):
        export([row()], chargeback=True, allocation_coverage="NaN")
    n, _out, _h = export([row()], chargeback=True, allocation_coverage="0.95")
    assert n == 1
    assert export([row()], allocation_coverage="0.1")[0] == 1     # gate only with --chargeback


def test_small_groups_merge_with_suppressed_users() -> None:
    rows = [row("payments", 8, 5_000_000_000), row("search", 6, 3_000_000_000),
            row("tiny", 2, 100_000_000), row("mini", 1, 50_000_000)]
    _n, out, _h = export(rows)
    teams = {json.loads(r["Tags"])["team"]: r for r in out}
    assert set(teams) == {"payments", other_label(5)}       # complementary suppression
    merged = teams[other_label(5)]
    assert merged["x_SuppressedUsers"] == "9"
    assert usd(merged["ListCost"]) == 3_150_000_000
    assert "cost_center" in json.loads(merged["Tags"])      # shared cost center survives
    assert teams["payments"]["x_SuppressedUsers"] == "0"


def test_identity_below_k_is_one_org_level_row() -> None:
    rows = [row("tiny", 2, 100), row("mini", 1, 50)]
    _n, out, _h = export(rows)
    assert len(out) == 1
    assert json.loads(out[0]["Tags"])["team"] == other_label(5)
    assert out[0]["ListCost"] == "0.00000015" and out[0]["x_SuppressedUsers"] == "3"


def test_users_unknown_rows_are_kept() -> None:
    _n, out, _h = export([row(None, 0, 7)])
    assert len(out) == 1 and "team" not in json.loads(out[0]["Tags"])


def test_owned_channels_are_replaced_by_extension_rows() -> None:
    ext = FocusRow(columns=(("ChargeCategory", "Usage"), ("ListCost", "12.5"), ("BilledCost", "10"),
                            ("x_DiscountPool", "2.5"), ("x_CreditsQuantity", "1250"),
                            ("PricingUnit", "AI Credits"), ("CommitmentDiscountId", "pool-1")),
                   channel="github_copilot", reconciled=True)
    rows = [row(), row(channel="github_copilot", path="copilot_pool", basis=Basis.LIST_EQUIVALENT)]
    _n, out, header = export(rows, extra_rows=[ext],
                             owned_channels=frozenset({"github_copilot"}))
    assert len(out) == 2
    assert [r["x_Channel"] for r in out] == ["anthropic_api", "github_copilot"]
    assert "x_DiscountPool" in header and "x_CreditsQuantity" in header
    assert "CommitmentDiscountId" in header
    assert header.index("CommitmentDiscountId") < header.index("x_Source")
    copilot = out[1]
    assert copilot["BilledCost"] == "10" and copilot["x_Source"] == "tokenbill"
    assert copilot["x_Reconciled"] == "true"
    unreconciled = dataclasses.replace(ext, reconciled=False)
    with pytest.raises(GateFailed):
        export([row()], extra_rows=[unreconciled])
    assert export([row()], extra_rows=[unreconciled], allow_unreconciled=True)[0] == 2
    assert export([row()], extra_rows=[ext], channels=frozenset({"anthropic_api"}))[0] == 1
    bad = FocusRow(columns=(("NotAFocusColumn", "1"),), channel="github_copilot", reconciled=True)
    with pytest.raises(ContractViolation):
        export([row()], extra_rows=[bad])


def test_contracted_and_recoverable_and_findings_columns() -> None:
    base = row()
    contracted = {F.row_key(base): 4_000_000_000}
    recoverable = {("payments", "main"): 1_500_000_000}
    _n, out, _h = export([base], contracted=contracted, recoverable_by_scope=recoverable,
                         findings=findings(), reconciliation=recon())
    r = out[0]
    assert r["ListCost"] == "5" and r["ContractedCost"] == r["BilledCost"] == "4"
    assert r["x_RecoverableCost"] == "1.5"
    assert r["x_FindingIds"].split(",") == ["f_1", "f_3"]
    assert r["x_TopWasteCause"] == "ttl-expiry"
    assert r["x_ReconciliationDeltaPct"] == "0.5025"
    with pytest.raises(ContractViolation):
        export([base], contracted={F.row_key(base): "4"})
    with pytest.raises(ContractViolation):
        export([base], recoverable_by_scope={("payments",): 1.5})


def test_contract_basis_rows() -> None:
    _n, out, _h = export([row(basis=Basis.CONTRACT)])
    assert out[0]["ContractedCost"] == out[0]["BilledCost"] == out[0]["ListCost"] == "5"


def test_split_rows_carry_the_allocation_method() -> None:
    split = AllocatedCostRow(**{f.name: getattr(row(), f.name)
                                for f in dataclasses.fields(LedgerCostRow)},
                             method_id="tokenbill.split.proportional.dev_days",
                             method_details='{"rule":"r1"}')
    _n, out, _h = export([split, row()])
    assert len(out) == 2
    methods = sorted(r["AllocatedMethodId"] for r in out)
    assert methods == ["", "tokenbill.split.proportional.dev_days"]


def test_request_buckets_and_estimated_only_rows() -> None:
    _n, out, _h = export([row(bucket="web_search", qty=4, nano=40_000_000),
                          row(bucket="cache_write_unknown", nano=0, low=100, high=200)])
    web = next(r for r in out if r["x_TokenBucket"] == "web_search")
    assert web["PricingUnit"] == "Requests" and web["PricingQuantity"] == "4"
    assert web["ListUnitPrice"] == "0.01"
    est = next(r for r in out if r["x_TokenBucket"] == "cache_write_unknown")
    assert est["ListCost"] == est["BilledCost"] == "0" and est["x_Evidence"] == "estimated"
    assert est["x_CacheTtl"] == "unknown"


def test_formula_injection_and_controls_are_neutralized() -> None:
    _n, out, _h = export([row(team="=HYPERLINK(1)", workspace_id="@evil\x1b[31m")])
    r = out[0]
    assert r["ResourceId"] == "'@evil"
    assert json.loads(r["Tags"])["team"] == "=HYPERLINK(1)"   # inside JSON, not a formula


def test_output_is_deterministic_and_sorted() -> None:
    rows = [row(date="2026-09-02"), row(date="2026-09-01"), row(team="search", users=5)]
    a = io.StringIO()
    b = io.StringIO()
    F.write_focus(rows, a, reconciled_channels=RECONCILED, allow_unreconciled=False,
                  rate_card_sha=SHA)
    F.write_focus(list(reversed(rows)), b, reconciled_channels=RECONCILED,
                  allow_unreconciled=False, rate_card_sha=SHA)
    assert a.getvalue() == b.getvalue()
    dates = [line.split(",")[5] for line in a.getvalue().splitlines()[1:]]
    assert dates == sorted(dates)


def test_argument_and_row_validation() -> None:
    with pytest.raises(UsageError):
        export([row()], k=0)
    with pytest.raises(UsageError):
        export([row()], rate_card_sha=None)
    with pytest.raises(UsageError):
        export([row(date="")])
    with pytest.raises(ContractViolation):
        export(["not a row"])
    with pytest.raises(ContractViolation):
        export([row()], extra_rows=["x"])


def test_unknown_channel_names() -> None:
    _n, out, _h = export([row(channel="mystery", path="api_key")], allow_unreconciled=True)
    assert out[0]["ServiceProviderName"] == "Anthropic"
    assert out[0]["ServiceName"] == "Anthropic (mystery)"


rows_strategy = st.lists(st.builds(
    lambda team, users, nano, date, bucket, basis: row(team, users, nano, date=date,
                                                       bucket=bucket, basis=basis),
    st.sampled_from(["a", "b", "c", "d", None]), st.integers(0, 12), st.integers(0, 10**12),
    st.sampled_from(["2026-09-01", "2026-09-02", "2026-12-31"]),
    st.sampled_from(["output", "cache_read", "web_search"]),
    st.sampled_from([Basis.LIST, Basis.LIST_EQUIVALENT])), max_size=25)


@settings(max_examples=120, deadline=None)
@given(rows_strategy, st.integers(1, 8))
def test_k_merging_preserves_every_days_total(rows: list[LedgerCostRow], k: int) -> None:
    _n, out, _h = export(rows, k=k)
    want: dict[str, int] = defaultdict(int)
    for r in rows:
        want[r.date_utc] += r.priced_nano
    got: dict[str, int] = defaultdict(int)
    for r in out:
        got[r["ChargePeriodStart"][:10]] += usd(r["ListCost"])
        team = json.loads(r["Tags"]).get("team")
        if team is not None and team != other_label(k):
            assert r["x_SuppressedUsers"] == "0"
    assert {d: n for d, n in got.items() if n} == {d: n for d, n in want.items() if n}


@settings(max_examples=80, deadline=None)
@given(st.lists(st.tuples(st.text(max_size=6), st.text(max_size=6)), max_size=6),
       st.text(max_size=6), st.booleans())
def test_extension_rows_only_raise_tokenbill_errors(columns, channel, reconciled) -> None:
    try:
        ext = FocusRow(columns=tuple(columns), channel=channel, reconciled=reconciled)
        export([], extra_rows=[ext], allow_unreconciled=True)
    except TokenbillError:
        pass

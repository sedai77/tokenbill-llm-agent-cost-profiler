"""Merge gate 1 (RECON, PLAN §1.5): ADMIN's recorded fixtures parsed by the real ADMIN adapters
reconcile with the real ``RateCard``. The same checks run with ``FakePricer`` (the ``facts.json``
rates RATES must equal) as soon as ADMIN is importable, so the seam is exercised before RATES
lands. Fixtures are ADMIN's checked-in files; ADMIN's test helpers are never imported (SPEC §21 #4).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tokenbill.core.ids import key_id
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import IngestOptions, IngestResult
from tokenbill.recon.reconcile import reconcile

pytestmark = pytest.mark.gate

ADMIN_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "admin"
MANIFEST: dict[str, Any] = json.loads((ADMIN_FIXTURES / "MANIFEST.json").read_text("utf-8"))
NAME_KEY = bytes(range(0, 32))
PRINCIPAL_KEY = bytes(range(32, 64))
TODAY = MANIFEST["options"]["now"]
E18 = Decimal("1E-18")


def _adapters() -> dict[str, Any]:
    admin = pytest.importorskip("tokenbill.adapters.anthropic_admin")
    cloud = pytest.importorskip("tokenbill.adapters.cloud_billing")
    openai = pytest.importorskip("tokenbill.adapters.openai_admin")
    return {"anthropic-usage-report": admin.UsageReportAdapter,
            "anthropic-cost-report": admin.CostReportAdapter,
            "anthropic-cc-analytics": admin.ClaudeCodeAnalyticsAdapter,
            "anthropic-enterprise-analytics": admin.EnterpriseAnalyticsAdapter,
            "openai-usage-buckets": openai.OpenAIUsageBucketsAdapter,
            "openai-costs": openai.OpenAICostsAdapter,
            "aws-cur": cloud.AwsCurAdapter, "gcp-billing": cloud.GcpBillingExportAdapter}


def _read(adapter: str, rel: str) -> IngestResult:
    options = MANIFEST["options"]
    opts = IngestOptions(identity_mode=options["identity_mode"], name_key=NAME_KEY,
                         name_key_id=key_id(NAME_KEY), principal_key=PRINCIPAL_KEY,
                         principal_key_id=key_id(PRINCIPAL_KEY),
                         team_map=tuple(sorted(options["team_map"].items())),
                         k_anonymity=options["k_anonymity"], now_ms=options["now_ms"])
    return _adapters()[adapter]().read(ADMIN_FIXTURES / rel, opts)


def _rate_card() -> Any:
    engine = pytest.importorskip("tokenbill.rates.engine")
    schema = pytest.importorskip("tokenbill.rates.schema")
    layer = schema.load_builtin()
    for build in (lambda: engine.RateCard([layer]), lambda: engine.RateCard((layer,)),
                  lambda: engine.RateCard(layers=[layer]), lambda: engine.RateCard(layer)):
        try:
            return build()
        except TypeError:
            continue
    pytest.fail("cannot build a RateCard from load_builtin(); update _rate_card()")


@pytest.fixture(params=["fake", "ratecard"])
def pricer(request: pytest.FixtureRequest) -> Any:
    _adapters()
    return FakePricer() if request.param == "fake" else _rate_card()


def _remainders(*results: IngestResult) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for r in results:
        value = r.stats.get("rounding_remainder_e18", 0)
        if value:
            out[r.source.adapter] = out.get(r.source.adapter, Decimal(0)) + value * E18
    return out


def test_recorded_pair_reconciles_to_the_nano(pricer: Any) -> None:
    pair = MANIFEST["recon_pairs"][0]
    usage = _read("anthropic-usage-report", pair["usage"])
    cost = _read("anthropic-cost-report", pair["cost"])
    report = reconcile([], usage.aggregates, cost.cost_lines, pricer, today=TODAY)
    assert {c.channel: c.verdict for c in report.channels} == {"anthropic_api": "reconciled"}
    assert report.channels[0].mapping_verified is True
    assert report.rate_card_error == ("0", "0", "0")
    joined = [r for r in report.rows
              if r.priced_provider_nano is not None and r.invoice_nano is not None]
    assert joined and all(r.priced_provider_nano == r.invoice_nano for r in joined)
    assert sum(r.invoice_nano or 0 for r in report.rows) == sum(
        c.amount_nano for c in cost.cost_lines)
    assert report.over_count_rows == 0 and report.unexplained_nano == 0


def test_edge_pair_residuals(pricer: Any) -> None:
    usage = _read("anthropic-usage-report", "anthropic/edge/usage_report_edge.json")
    cost = _read("anthropic-cost-report", "anthropic/edge/cost_report_edge.json")
    report = reconcile([], usage.aggregates, cost.cost_lines, pricer, today=TODAY,
                       rounding_remainders=_remainders(usage, cost))
    codes = dict(report.residuals)
    assert codes["priority_excluded_from_cost_report"] > 0
    assert codes["code_execution_cost_report_only"] > 0
    assert "cents_rounding" in codes            # the 4.5e-15 USD sub-nano remainder
    assert {c.channel: c.verdict for c in report.channels} == {"anthropic_api": "reconciled"}


def test_every_admin_fixture_through_one_reconciliation(pricer: Any) -> None:
    results = [_read(entry["adapter"], entry["path"]) for entry in MANIFEST["files"]
               if not entry["path"].endswith(".gz")]
    aggregates = [a for r in results for a in r.aggregates]
    lines = [c for r in results for c in r.cost_lines]
    report = reconcile([], aggregates, lines, pricer, today=TODAY,
                       rounding_remainders=_remainders(*results))
    by_channel = {c.channel: c for c in report.channels}
    assert set(by_channel) >= {"anthropic_api", "bedrock", "vertex", "openai_api"}
    assert by_channel["anthropic_api"].invoice_sources == ("anthropic.cost_report",)
    for channel in ("bedrock", "vertex", "openai_api"):
        assert by_channel[channel].mapping_verified is False     # totals only; schema unverified
    assert by_channel["bedrock"].invoice_sources == ("aws.cur2",)
    usage_lines = [c for c in lines if c.channel == "bedrock" and c.cost_type is None]
    net = sum(c.amount_nano for c in usage_lines)
    listed = sum(c.list_amount_nano or 0 for c in usage_lines)
    expected = (1 - Decimal(net) / Decimal(listed)).quantize(Decimal("0.000001"))
    assert Decimal(dict(report.effective_discount)["bedrock:*:*"]) == expected
    credits = [r for r in report.rows if ("bucket", "credits") in r.key]
    assert credits and all(r.invoice_nano < 0 for r in credits)   # CUR Credit line items
    # September CUR rows are inside the revision window of the manifest's "now"
    assert {r.residual_code for r in credits} == {"revision_window"}
    assert report.verdict in ("reconciled", "not_reconciled", "insufficient_data")

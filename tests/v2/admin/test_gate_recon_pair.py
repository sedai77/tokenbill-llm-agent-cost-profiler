"""Merge gate 1 (ADMIN side of the RECON seam): the recorded usage/cost pair parsed by the ADMIN
adapters is priced to the nano by the real ``RateCard`` and reconciles with the real
``recon.reconcile`` (SPEC §12.1 layer 1, PLAN §1.5). Skips until RATES / RECON are merged."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

import pytest

from tokenbill.adapters.anthropic_admin import date_of
from tokenbill.core.builders import make_ctx

from .helpers import MANIFEST, dims, fixture, read

pytestmark = pytest.mark.gate

PAIR = MANIFEST["recon_pairs"][0]
_TOKEN_TYPE = {"uncached_input": "uncached_input_tokens", "output": "output_tokens",
               "cache_read": "cache_read_input_tokens",
               "cache_write_5m": "cache_creation.ephemeral_5m_input_tokens",
               "cache_write_1h": "cache_creation.ephemeral_1h_input_tokens"}


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
    pytest.fail("cannot build a RateCard from load_builtin()")


def test_rate_card_prices_the_pair_to_the_nano() -> None:
    pricer = _rate_card()
    usage = read("anthropic-usage-report", fixture(PAIR["usage"]))
    cost = read("anthropic-cost-report", fixture(PAIR["cost"]))
    priced: dict[tuple, int] = defaultdict(int)
    for agg in usage.aggregates:
        d = dims(agg)
        ctx = make_ctx(d["model"], channel=d["channel"], service_tier=d["service_tier"],
                       inference_geo=d.get("inference_geo"))
        result = pricer.price_usage(agg.usage, ctx, ts_ms=agg.bucket_start_ms)
        assert result.unpriced_reason is None
        for line in result.lines:
            if line.bucket.startswith("web_search"):
                key = (date_of(agg.bucket_start_ms), d.get("workspace_id"), None, "web_search")
            else:
                key = (date_of(agg.bucket_start_ms), d.get("workspace_id"), d["model"],
                       _TOKEN_TYPE[line.bucket])
            priced[key] += line.amount_nano
    invoiced = {(c.date_utc, c.workspace_id, c.model,
                 c.token_type if c.cost_type == "tokens" else c.cost_type): c.amount_nano
                for c in cost.cost_lines}
    assert invoiced == {k: v for k, v in priced.items() if v}


def test_real_reconcile_verdict() -> None:
    recon = pytest.importorskip("tokenbill.recon.reconcile")
    pricer = _rate_card()
    usage = read("anthropic-usage-report", fixture(PAIR["usage"]))
    cost = read("anthropic-cost-report", fixture(PAIR["cost"]))
    report = recon.reconcile([], usage.aggregates, cost.cost_lines, pricer,
                             today=MANIFEST["options"]["now"])
    assert report.over_count_rows == 0
    for row in report.rows:
        if row.priced_provider_nano is not None and row.invoice_nano is not None:
            assert row.priced_provider_nano == row.invoice_nano, row.key
            if row.rate_card_error_pct is not None:
                assert Decimal(row.rate_card_error_pct) == 0
    verdicts = {c.channel: c.verdict for c in report.channels}
    assert verdicts.get("anthropic_api") == "reconciled"

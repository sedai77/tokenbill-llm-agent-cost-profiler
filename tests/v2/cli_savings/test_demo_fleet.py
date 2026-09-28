"""Synthetic fleet demo pricing safeguards."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from tokenbill.core.builders import make_ctx
from tokenbill.core.labels import Basis
from tokenbill.core.records import UsageBuckets
from tokenbill.pipeline import savings
from tokenbill.rates.engine import RateCard


def _ts(date: str) -> int:
    return (dt.date.fromisoformat(date) - dt.date(1970, 1, 1)).days * savings.DAY_MS


def test_embedded_demo_contract_applies_only_to_anthropic_api() -> None:
    factory = savings._synthetic_demo_pricer_factory(
        effective_from="2026-09-22",
        multiplier=Decimal("0.85"),
    )
    pricer = factory(rates=(), contract=None, model_prices=())

    assert isinstance(pricer, RateCard)
    assert pricer.contract is not None
    assert pricer.contract.channels == ("anthropic_api",)
    assert pricer.contract.multiplier == Decimal("0.85")

    usage = UsageBuckets(uncached_input=1_000_000)
    anthro = pricer.price_usage(usage, make_ctx("claude-opus-5-5"), ts_ms=_ts("2026-09-22"))
    bedrock = pricer.price_usage(
        usage,
        make_ctx("claude-opus-5-5", channel="bedrock", endpoint_scope="regional"),
        ts_ms=_ts("2026-09-22"),
    )

    assert anthro.figure.basis is Basis.CONTRACT
    assert anthro.figure.nano == 3_400_000_000
    assert bedrock.figure.basis is Basis.LIST

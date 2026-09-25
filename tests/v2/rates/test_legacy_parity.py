"""SPEC §6.8 legacy parity: every v0.1 ``pricing.PRICING`` row equals the built-in
``anthropic_api`` registry row effective on 2026-09-23 (input, output, read multiplier, 5m and 1h
write multipliers, minimum cacheable prefix)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tokenbill import pricing
from tokenbill.rates.engine import RateCard

from .helpers import builtin, ctx, ts


def _dec(value: float) -> Decimal:
    return Decimal(repr(value))


@pytest.mark.parametrize("model", sorted(pricing.PRICING))
def test_pricing_row_equals_the_registry_row(model: str) -> None:
    legacy = pricing.PRICING[model]
    card = RateCard([builtin()])
    rows = [r for r in builtin().rows if r.channel == "anthropic_api" and r.enabled
            and r.model == model and r.effective_from <= "2026-09-23"
            and (r.effective_to is None or "2026-09-23" < r.effective_to)]
    assert len(rows) == 1, model
    row = rows[0]
    assert card.resolve(ctx(model), ts_ms=ts("2026-09-23")) is not None
    assert row.input_usd_per_mtok == _dec(legacy.input_per_mtok)
    assert row.output_usd_per_mtok == _dec(legacy.output_per_mtok)
    assert row.cache_read_mult == _dec(legacy.cache_read_multiplier)
    assert row.cache_write_5m_mult == _dec(legacy.cache_write_multiplier)
    assert row.cache_write_1h_mult == _dec(legacy.cache_write_1h_multiplier)
    assert row.min_cacheable_tokens == legacy.min_cacheable_prefix_tokens


def test_every_legacy_model_has_a_registry_row() -> None:
    names = {r.model for r in builtin().rows if r.channel == "anthropic_api"}
    assert set(pricing.PRICING) <= names

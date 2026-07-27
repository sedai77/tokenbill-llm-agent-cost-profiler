"""pricing.py: SPEC tables verbatim, exact dollar math, honest unknown-model path.

These tests duck-type usage on purpose (pricing only reads the four token
fields), so they run even while trace.py is being written by another module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest

from tokenbill import pricing


@dataclass(frozen=True)
class _Usage:
    """Minimal usage double: pricing only reads these four fields."""

    input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0


# The authoritative table from docs/SPEC.md (source: platform.claude.com
# pricing doc, verified 2026-07). A drift here must fail loudly.
SPEC_TABLE = {
    "claude-opus-5": (5.00, 25.00, 512),
    "claude-fable-5": (10.00, 50.00, 512),
    "claude-opus-4-8": (5.00, 25.00, 1024),
    "claude-opus-4-7": (5.00, 25.00, 2048),
    "claude-opus-4-6": (5.00, 25.00, 4096),
    "claude-sonnet-5": (3.00, 15.00, 1024),
    "claude-sonnet-4-6": (3.00, 15.00, 1024),
    "claude-haiku-4-5": (1.00, 5.00, 4096),
}


def test_pricing_table_matches_spec_exactly() -> None:
    assert set(pricing.PRICING) == set(SPEC_TABLE)
    for model, (input_rate, output_rate, min_prefix) in SPEC_TABLE.items():
        entry = pricing.PRICING[model]
        assert entry.input_per_mtok == input_rate, model
        assert entry.output_per_mtok == output_rate, model
        assert entry.min_cacheable_prefix_tokens == min_prefix, model


def test_cache_multiplier_defaults_apply_to_every_entry() -> None:
    for model, entry in pricing.PRICING.items():
        assert entry.cache_write_multiplier == 1.25, model
        assert entry.cache_read_multiplier == 0.10, model


def test_cache_rule_constants() -> None:
    assert pricing.CACHE_TTL_SECONDS == 300
    assert pricing.TTL_REFRESH_ON_READ is True
    assert pricing.MAX_BREAKPOINTS == 4
    assert pricing.RENDER_ORDER == ("tools", "system", "messages")


def test_cost_breakdown_arithmetic_sonnet_5() -> None:
    usage = _Usage(
        input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    breakdown = pricing.cost_breakdown("claude-sonnet-5", usage)
    assert breakdown is not None
    assert breakdown["uncached"] == pytest.approx(3.00)
    assert breakdown["write"] == pytest.approx(3.00 * 1.25)  # 3.75
    assert breakdown["read"] == pytest.approx(3.00 * 0.10)  # 0.30
    assert breakdown["output"] == pytest.approx(15.00)
    assert set(breakdown) == {"uncached", "write", "read", "output"}


def test_price_usd_is_breakdown_sum() -> None:
    usage = _Usage(
        input_tokens=123_456,
        cache_read_input_tokens=700_000,
        cache_creation_input_tokens=45_000,
        output_tokens=9_876,
    )
    for model in pricing.PRICING:
        breakdown = pricing.cost_breakdown(model, usage)
        assert breakdown is not None
        assert pricing.price_usd(model, usage) == pytest.approx(sum(breakdown.values()))


def test_price_usd_fable_5_mixed_usage() -> None:
    # 10.00 in/MTok: read at 0.10x = 1.00/MTok, write at 1.25x = 12.50/MTok.
    usage = _Usage(
        input_tokens=200_000,
        cache_read_input_tokens=500_000,
        cache_creation_input_tokens=100_000,
        output_tokens=40_000,
    )
    expected = 0.2 * 10.00 + 0.5 * 1.00 + 0.1 * 12.50 + 0.04 * 50.00
    assert pricing.price_usd("claude-fable-5", usage) == pytest.approx(expected)


def test_zero_usage_prices_to_zero() -> None:
    assert pricing.price_usd("claude-haiku-4-5", _Usage()) == pytest.approx(0.0)


def test_unknown_model_returns_none_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    usage = _Usage(input_tokens=10)
    with caplog.at_level(logging.WARNING, logger="tokenbill.pricing"):
        assert pricing.price_usd("gpt-oss-999", usage) is None
        assert pricing.cost_breakdown("gpt-oss-999", usage) is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "unknown model must log a warning"
    assert "gpt-oss-999" in warnings[0].getMessage()


def test_unknown_model_warns_once_per_model(caplog: pytest.LogCaptureFixture) -> None:
    # Regression: every lookup used to warn, so a few-hundred-call trace of
    # one unknown model flooded stderr with identical lines. Dedupe is per
    # model id: repeat lookups are silent, a second model still warns.
    usage = _Usage(input_tokens=10)
    with caplog.at_level(logging.WARNING, logger="tokenbill.pricing"):
        for _ in range(4):
            assert pricing.price_usd("dedupe-model-a", usage) is None
            assert pricing.cost_breakdown("dedupe-model-a", usage) is None
        assert pricing.price_usd("dedupe-model-b", usage) is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len([w for w in warnings if "dedupe-model-a" in w]) == 1
    assert len([w for w in warnings if "dedupe-model-b" in w]) == 1


def test_model_pricing_dataclass_defaults() -> None:
    entry = pricing.ModelPricing(input_per_mtok=1.0, output_per_mtok=2.0)
    assert entry.cache_write_multiplier == 1.25
    assert entry.cache_read_multiplier == 0.10
    assert entry.min_cacheable_prefix_tokens == 1024

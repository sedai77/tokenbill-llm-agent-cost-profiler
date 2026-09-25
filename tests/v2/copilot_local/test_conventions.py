"""The three Copilot CLI conventions, the exact nano-AIU sum-check and the §5.11 billing rules
(Appendix C G11, G12)."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from tokenbill.adapters import copilot_conventions as cc
from tokenbill.core import conventions
from tokenbill.core.builders import make_copilot_ctx
from tokenbill.core.conventions import BadUsageError
from tokenbill.core.money import nano_aiu_to_nano
from tokenbill.core.records import Inference, InferenceKind, UsageBuckets
from tokenbill.core.testing import FakePricer

from .helpers import G11_DETAILS, G11_TOTAL_NANO_AIU, T0


def test_registered_through_core_conventions() -> None:
    for cid, inclusive in ((cc.CONVENTION_SHUTDOWN_ROLLUP, True),
                           (cc.CONVENTION_SESSION_STORE, True),
                           (cc.CONVENTION_TOKEN_DETAILS, False)):
        conv = conventions.get_convention(cid)
        assert conv.provider == "github" and conv.enabled and conv.inclusive_input is inclusive
    buckets, notes = conventions.normalize(cc.CONVENTION_SHUTDOWN_ROLLUP, {
        "inputTokens": 23_399, "cacheReadTokens": 10_069, "cacheWriteTokens": 13_324,
        "outputTokens": 250})
    assert (buckets.uncached_input, buckets.cache_read, buckets.cache_write_unknown) == (
        6, 10_069, 13_324) and notes == []


def test_shutdown_rollup_mismatch_and_reasoning() -> None:
    buckets, notes = cc.normalize_shutdown_rollup({"inputTokens": 10, "cacheReadTokens": 8,
                                                   "cacheWriteTokens": 5, "outputTokens": 4,
                                                   "reasoningTokens": 9})
    assert buckets.uncached_input == 10 and buckets.output_reasoning is None
    assert notes == [cc.DQ_CONVENTION_MISMATCH, "dq.sum_check_failed"]
    ok, _ = cc.normalize_shutdown_rollup({"outputTokens": 4, "reasoningTokens": 2})
    assert ok.output_reasoning == 2 and ok.total_input == 0
    for bad in ({"inputTokens": -1}, {"inputTokens": "7"}, {"outputTokens": True},
                {"cacheReadTokens": 2**60}):
        with pytest.raises(BadUsageError):
            cc.normalize_shutdown_rollup(bad)
    with pytest.raises(BadUsageError):
        cc.normalize_shutdown_rollup([1, 2])  # type: ignore[arg-type]


def test_session_store_negative_uncached_raises() -> None:
    buckets, notes = cc.normalize_session_store({"input_tokens": 100, "cache_read_tokens": 60,
                                                 "cache_write_tokens": 10, "output_tokens": 7})
    assert (buckets.uncached_input, buckets.cache_read, buckets.cache_write_unknown,
            buckets.output) == (30, 60, 10, 7) and notes == []
    with pytest.raises(cc.NegativeUncachedError):
        cc.normalize_session_store({"input_tokens": 10, "cache_read_tokens": 60})
    with pytest.raises(BadUsageError):
        cc.normalize_session_store("row")  # type: ignore[arg-type]


def test_token_details_g11_exact() -> None:
    usage = {"tokenDetails": G11_DETAILS, "totalNanoAiu": G11_TOTAL_NANO_AIU}
    buckets, notes = cc.normalize_token_details(usage)
    assert (buckets.uncached_input, buckets.cache_read, buckets.cache_write_unknown,
            buckets.output) == (6, 127_386, 2_220, 6_210)
    assert notes == []
    assert cc.token_details_nano_aiu(usage) == G11_TOTAL_NANO_AIU
    assert nano_aiu_to_nano(G11_TOTAL_NANO_AIU) == (232_848_000, 0)
    # our point at the Opus 4.7 rates equals the provider figure (the write billed at the
    # published price)
    inf = Inference(inference_id="i", kind=InferenceKind.COMPACTION, usage=buckets,
                    pricing=make_copilot_ctx("claude-opus-4-7"))
    assert FakePricer().price_inference(inf, ts_ms=T0).figure.nano == 232_848_000


def test_token_details_sum_check_and_rounding() -> None:
    details = [{"tokenType": "input", "tokenCount": 3, "batchSize": 2,
                "costPerBatch": Decimal("1.5")}]                     # 2.25 nano-AIU
    assert cc.token_details_nano_aiu({"tokenDetails": details}) == Fraction(9, 4)
    _, near = cc.normalize_token_details({"tokenDetails": details, "totalNanoAiu": 2})
    assert near == []                                                # < 1 nano-AIU: rounding
    _, far = cc.normalize_token_details({"tokenDetails": details, "totalNanoAiu": 4})
    assert far == [cc.DQ_NANO_AIU_MISMATCH]
    _, skipped = cc.normalize_token_details({"tokenDetails": details})
    assert skipped == []
    floats = [{"tokenType": "output", "tokenCount": 2, "batchSize": 1, "costPerBatch": 0.5}]
    assert cc.token_details_nano_aiu({"tokenDetails": floats}) == 1
    twice = cc.normalize_token_details({"tokenDetails": G11_DETAILS + G11_DETAILS[:1]})[0]
    assert twice.uncached_input == 12


@pytest.mark.parametrize("usage", [
    {"tokenDetails": "x"},
    {"tokenDetails": [1]},
    {"tokenDetails": [{"tokenType": "reasoning", "tokenCount": 1}]},
    {"tokenDetails": [{"tokenType": "input", "tokenCount": -1}]},
    {"tokenDetails": [{"tokenType": "input", "tokenCount": 1, "batchSize": 0,
                       "costPerBatch": 1}], "totalNanoAiu": 1},
    {"tokenDetails": [{"tokenType": "input", "tokenCount": 1, "batchSize": 1,
                       "costPerBatch": "1"}], "totalNanoAiu": 1},
    {"tokenDetails": [], "totalNanoAiu": "many"},
    {"tokenDetails": [{"tokenType": "input", "tokenCount": 2**53}] * 2},
    {"tokenDetails": [{"tokenType": "input", "tokenCount": 1}] * 300},
])
def test_token_details_malformed(usage) -> None:
    with pytest.raises(BadUsageError):
        cc.normalize_token_details(usage)


def test_token_details_not_a_mapping() -> None:
    with pytest.raises(BadUsageError):
        cc.normalize_token_details(None)  # type: ignore[arg-type]


def test_exact_number() -> None:
    assert cc.exact_number(3) == 3 and cc.exact_number(Decimal("0.25")) == Fraction(1, 4)
    assert cc.exact_number(0.5) == Fraction(1, 2)
    for bad in (-1, True, "1", None, Decimal("NaN"), Decimal("-2"), [1]):
        assert cc.exact_number(bad) is None


def test_billing_rules_g12() -> None:
    assert "gpt-4o-mini" in cc.utility_models()
    # G12: utility model with nano-AIU 0 on a background call → unbilled, EXACT $0
    billable, rule = cc.billing_rule("gpt-4o-mini", nano_aiu=0,
                                     interaction_type="conversation-background")
    assert (billable, rule) == (False, cc.UTILITY_RULE)
    inf = Inference(inference_id="g12", kind=InferenceKind.MESSAGE,
                    usage=UsageBuckets(uncached_input=900, output=40),
                    pricing=make_copilot_ctx("gpt-4o-mini"), billable=billable,
                    billing_rule_id=rule)
    priced = FakePricer().price_inference(inf, ts_ms=T0)
    assert priced.figure.nano == 0 and priced.unpriced_reason is None
    # never by name alone: GPT-5.4 nano is billed when it reports credits
    assert cc.billing_rule("gpt-5.4-nano", nano_aiu=12) == (True, None)
    assert cc.billing_rule("gpt-4o-mini", nano_aiu=None) == (True, None)
    assert cc.billing_rule("claude-opus-4-7", nano_aiu=0,
                           interaction_type="conversation-background") == (False, cc.UTILITY_RULE)
    assert cc.billing_rule("claude-opus-4-7", nano_aiu=0) == (True, None)
    assert cc.billing_rule("gpt-5.4", nano_aiu=99, is_byok=True) == (False, cc.BYOK_RULE)

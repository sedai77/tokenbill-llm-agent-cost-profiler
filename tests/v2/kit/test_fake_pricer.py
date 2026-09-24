"""FakePricer: SPEC §6.9 golden cases, §6.2–§6.4 rules, conformance (F-KIT acceptance)."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core import testing as kit
from tokenbill.core.builders import FlatRates, make_ctx, make_inference
from tokenbill.core.errors import PricingError, UsageError
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import UsageBuckets, UsageSource
from tokenbill.core.types import ContractOverlay

TS = kit._ts("2026-09-23")
TS_0910 = kit._ts("2026-09-10")
CASE1 = UsageBuckets(uncached_input=1000, cache_read=100_000, cache_write_5m=2000,
                     cache_write_1h=3000, output=500)


def price(usage: UsageBuckets, model: str = "claude-opus-5-5", ts: int = TS, *,
          pricer: kit.FakePricer | None = None, **ctx):
    return (pricer or kit.FakePricer()).price_usage(usage, make_ctx(model, **ctx), ts_ms=ts)


def contract(multiplier: str | None = "0.85", **kw) -> ContractOverlay:
    base = {"name": "acme", "multiplier": Decimal(multiplier) if multiplier else None,
            "overrides": (), "effective_from": "2026-01-01", "effective_to": None,
            "derived": False, "assumed_fields": ()}
    base.update(kw)
    return ContractOverlay(**base)


# ---------- conformance ----------


def test_fake_pricer_conforms() -> None:
    summary = kit.assert_pricer_conforms(kit.FakePricer())
    assert summary["unit_rate_samples"] == 200
    assert set(summary["golden_cases"]) >= {"1", "2", "3", "4", "5", "6", "7", "9", "10", "18",
                                            "21", "23", "24"}


def test_flat_rates_conforms_and_skips_golden_rows() -> None:
    summary = kit.assert_pricer_conforms(FlatRates())
    assert summary == {"golden_cases": [], "unit_rate_samples": 200}


def test_contract_pricer_conforms() -> None:
    summary = kit.assert_pricer_conforms(kit.FakePricer().with_contract(contract()), samples=50)
    assert summary["golden_cases"] == [] and summary["unit_rate_samples"] == 50


# ---------- SPEC §6.9 cases, one by one ----------


def test_case_1_to_4_and_18() -> None:
    assert price(CASE1).figure.nano == 68_000_000
    assert price(CASE1, inference_geo="us").figure.nano == 74_800_000
    assert price(CASE1, service_tier="batch").figure.nano == 34_000_000
    assert price(CASE1, service_tier="batch", inference_geo="us").figure.nano == 37_400_000
    fast = price(CASE1, speed="fast")
    assert fast.figure.nano == 136_000_000
    assert "anthropic.fast.opus-5-5" in fast.lines[0].modifier_ids
    big = price(UsageBuckets(cache_write_1h=1_000_000), service_tier="batch", inference_geo="us")
    assert big.figure.nano == 4_400_000_000
    assert big.figure.evidence is Evidence.EXACT and big.figure.is_billed_eligible


def test_case_1_lines_are_rounded_once_each() -> None:
    p = price(CASE1)
    amounts = {ln.bucket: (ln.amount_nano, ln.unit_usd_per_mtok, ln.exact) for ln in p.lines}
    assert amounts == {
        "uncached_input": (4_000_000, "4", True),
        "cache_read": (20_000_000, "0.2", True),
        "cache_write_5m": (10_000_000, "5", True),
        "cache_write_1h": (24_000_000, "8", True),
        "output": (10_000_000, "20", True),
    }
    assert p.exact_nano == 68_000_000 and p.estimated is None and p.unpriced_reason is None
    assert all(ln.rate_row_id == "anthropic/anthropic_api/claude-opus-5-5/2026-09-22"
               and ln.layer == "builtin" for ln in p.lines)


def test_case_5_fable_read_multipliers() -> None:
    reads = UsageBuckets(cache_read=1_000_000)
    assert price(reads, "claude-fable-5-1", TS_0910).figure.nano == 250_000_000
    assert price(reads, "claude-fable-5", TS_0910).figure.nano == 1_000_000_000
    assert price(reads, "claude-mythos-5-1", TS_0910).figure.nano == 250_000_000


def test_case_6_web_search() -> None:
    p = price(dataclasses.replace(CASE1, web_search_requests=3))
    web = [ln for ln in p.lines if ln.bucket == "web_search"]
    assert p.figure.nano == 98_000_000
    assert web[0].quantity == 3 and web[0].unit_usd_per_mtok == "0.01" and web[0].exact


def test_case_7_bedrock_scopes() -> None:
    inp = UsageBuckets(uncached_input=1_000_000)
    g = price(inp, "claude-opus-5", channel="bedrock", endpoint_scope="global")
    r = price(inp, "claude-opus-5", channel="bedrock", endpoint_scope="regional")
    m = price(inp, "claude-opus-5", channel="bedrock", endpoint_scope="multi_region")
    u = price(inp, "claude-opus-5", channel="bedrock", endpoint_scope="unknown")
    assert g.figure.nano == 5_000_000_000 and g.figure.evidence is Evidence.EXACT
    assert r.figure.nano == m.figure.nano == 5_500_000_000
    assert "bedrock.endpoint.regional" in r.lines[0].modifier_ids
    assert u.figure.evidence is Evidence.ESTIMATED and u.exact_nano == 0
    assert (u.figure.nano, u.figure.low_nano, u.figure.high_nano) == (
        5_000_000_000, 5_000_000_000, 5_500_000_000)
    assert "endpoint scope unknown" in u.figure.note
    resolved = kit.FakePricer().resolve(make_ctx("claude-opus-5", channel="bedrock"), ts_ms=TS)
    assert resolved is not None and resolved.scope_range is not None
    assert resolved.scope_range.input == Decimal("5.50")


def test_case_7_unknown_scope_ranges_every_line() -> None:
    usage = UsageBuckets(uncached_input=1000, cache_read=2000, cache_write_unknown=3000, output=40,
                         web_search_requests=0)
    p = price(usage, "claude-opus-5", channel="bedrock", endpoint_scope="unknown")
    assert all(not ln.exact for ln in p.lines)
    placeholder = kit.FakePricer().price_usage(
        UsageBuckets(output=5), make_ctx("claude-opus-5", channel="bedrock"), ts_ms=TS,
        usage_source=UsageSource.MESSAGE_START_ONLY, output_upper=100)
    line = placeholder.lines[0]
    assert (line.low_nano, line.high_nano) == (125_000, 2_750_000)


def test_case_8_and_19_contract() -> None:
    card = kit.FakePricer().with_contract(contract())
    p = card.price_usage(CASE1, make_ctx("claude-opus-5-5"), ts_ms=TS)
    assert p.figure.nano == 57_800_000 and p.figure.basis is Basis.CONTRACT
    assert card.basis is Basis.CONTRACT and all(ln.layer == "contract" for ln in p.lines)
    assert "contract:acme" in p.lines[0].modifier_ids
    odd = kit.FakePricer().with_contract(contract("0.8537"))
    q = odd.price_usage(UsageBuckets(output=3), make_ctx("claude-opus-4-8"), ts_ms=TS)
    assert q.figure.nano == 64_028
    assert card.rate_card_sha256 != kit.FakePricer().rate_card_sha256


def test_case_9_unknown_ttl_range() -> None:
    p = price(UsageBuckets(uncached_input=1000, cache_write_unknown=1_000_000, output=500))
    assert p.exact_nano == 14_000_000
    assert p.estimated is not None
    assert (p.estimated.nano, p.estimated.low_nano, p.estimated.high_nano) == (
        5_000_000_000, 5_000_000_000, 8_000_000_000)
    assert p.estimated.high_nano - p.estimated.low_nano == 3_000_000_000
    hinted = price(UsageBuckets(cache_write_unknown=1_000_000), write_ttl_hint="1h")
    assert (hinted.figure.nano, hinted.figure.low_nano, hinted.figure.high_nano) == (
        8_000_000_000, 5_000_000_000, 8_000_000_000)


def test_case_10_and_11_openai() -> None:
    usage = UsageBuckets(uncached_input=2000, cache_read=6000, cache_write_other=2000,
                         cache_write_other_ttl_s=1800, output=1000)
    assert price(usage, "gpt-5.6-sol", TS_0910).figure.nano == 40_400_000
    big = price(UsageBuckets(uncached_input=300_000, output=1000), "gpt-5.6-sol", TS_0910)
    assert big.figure.nano == 2_430_000_000
    assert {ln.unit_usd_per_mtok for ln in big.lines} == {"8", "30"}
    at_threshold = price(UsageBuckets(uncached_input=272_000), "gpt-5.6-sol", TS_0910)
    assert at_threshold.figure.nano == 272_000 * 4_000
    resolved = kit.FakePricer().resolve(make_ctx("gpt-5.6-sol"), ts_ms=TS_0910)
    assert resolved is not None and not resolved.long_context_band


def test_case_15_17_21_22_unpriced() -> None:
    cases = [
        (price(UsageBuckets(output=1), "claude-foo-9"), "no rate row"),
        (price(UsageBuckets(output=1), ts=kit._ts("2026-09-21")), "model before effective date"),
        (price(UsageBuckets(output=1), "gpt-5.6-sol", kit._ts("2026-08-01")),
         "unverified rate row"),
        (price(UsageBuckets(output=1), "gpt-5.6-sol", kit._ts("2026-11-22")), "promotion expired"),
        (price(UsageBuckets(cache_read=1_000_000), "claude-fable-5-1", kit._ts("2026-08-20")),
         "model before effective date"),
        (price(UsageBuckets(output=1), ""), "not priceable"),
    ]
    for p, reason in cases:
        assert p.figure.nano is None and p.unpriced_reason == reason
        assert p.figure.note == f"unpriced: {reason}" and p.lines == () and p.exact_nano == 0
        assert reason in kit.UNPRICED_DQ
    assert kit.UNPRICED_DQ["promotion expired"] == "dq.promotion_expired"
    last_day = price(UsageBuckets(output=1_000_000), "gpt-5.6-sol", kit._ts("2026-11-21"))
    assert last_day.figure.nano == 20_000_000_000


def test_case_23_subscription_basis() -> None:
    p = price(CASE1, billing_path="subscription")
    assert p.figure.nano == 68_000_000 and p.figure.basis is Basis.LIST_EQUIVALENT
    assert p.figure.evidence is Evidence.EXACT and not p.figure.is_billed_eligible
    card = kit.FakePricer().with_contract(contract())
    q = card.price_usage(CASE1, make_ctx("claude-opus-5-5", billing_path="subscription"),
                         ts_ms=TS)
    assert q.figure.nano == 68_000_000 and q.figure.basis is Basis.LIST_EQUIVALENT
    total = kit.fake_price_total(kit.FakePricer(), [
        (make_inference(CASE1, billing_path="subscription"), TS),
        (make_inference(CASE1, inference_id="inf_b"), TS),
    ])
    assert total.exact.nano == 68_000_000 and total.exact.basis is Basis.LIST
    assert total.allowance is not None and total.allowance.nano == 68_000_000
    assert total.allowance.basis is Basis.LIST_EQUIVALENT and total.estimated is None
    assert (total.priced_inferences, total.unpriced_inferences, total.coverage) == (2, 0, "1")


def test_case_24_placeholder_output() -> None:
    p = kit.FakePricer().price_usage(UsageBuckets(cache_read=100_000, output=3),
                                     make_ctx("claude-opus-5-5"), ts_ms=TS,
                                     usage_source=UsageSource.MESSAGE_START_ONLY,
                                     output_upper=403)
    out = next(ln for ln in p.lines if ln.bucket == "output")
    assert p.exact_nano == 20_000_000
    assert (out.amount_nano, out.low_nano, out.high_nano, out.exact) == (
        60_000, 60_000, 8_060_000, False)
    assert p.figure.evidence is Evidence.ESTIMATED
    zero_logged = kit.FakePricer().price_usage(
        UsageBuckets(cache_read=10), make_ctx("claude-opus-5-5"), ts_ms=TS,
        usage_source=UsageSource.MESSAGE_START_ONLY, output_upper=100)
    assert zero_logged.lines[-1].bucket == "output" and zero_logged.lines[-1].amount_nano == 0
    assert zero_logged.lines[-1].high_nano == 2_000_000


# ---------- other resolution rules ----------


def test_channel_fallback_and_geo_on_foundry() -> None:
    usage = UsageBuckets(uncached_input=1_000_000)
    foundry = price(usage, channel="foundry", inference_geo="us")
    assert foundry.figure.nano == 4_400_000_000  # US geo applies to foundry (Appendix E)
    platform = price(usage, channel="claude_platform_aws")
    assert platform.figure.nano == 4_000_000_000
    assert price(usage, channel="vertex").unpriced_reason == "no rate row"
    batch_openai = price(UsageBuckets(output=1_000_000), "gpt-5.6-sol", TS_0910,
                         service_tier="batch")
    assert batch_openai.figure.nano == 20_000_000_000  # Anthropic batch modifier only


def test_haiku_is_below_the_geo_generation() -> None:
    p = price(UsageBuckets(uncached_input=1_000_000), "claude-haiku-4-5", inference_geo="us")
    assert p.figure.nano == 1_000_000_000 and p.lines[0].modifier_ids == ()


def test_billing_rules_per_line() -> None:
    unsure = kit.FakePricer().price_usage(CASE1, make_ctx("claude-opus-5-5"), ts_ms=TS,
                                          billable=None)
    assert (unsure.figure.low_nano, unsure.figure.nano, unsure.figure.high_nano) == (
        0, 68_000_000, 68_000_000)
    assert "billing uncertain" in unsure.figure.note
    declined = kit.FakePricer().price_usage(CASE1, make_ctx("claude-opus-5-5"), ts_ms=TS,
                                            billable=False)
    assert declined.figure.nano == 0 and declined.figure.evidence is Evidence.EXACT
    assert len(declined.lines) == 5 and all(ln.amount_nano == 0 for ln in declined.lines)
    recon = kit.FakePricer().price_usage(CASE1, make_ctx("claude-opus-5-5"), ts_ms=TS,
                                         usage_source=UsageSource.ESTIMATED)
    assert recon.figure.low_nano == recon.figure.high_nano == 68_000_000
    assert "reconstructed" in recon.figure.note
    partial = kit.FakePricer().price_usage(CASE1, make_ctx("claude-opus-5-5"), ts_ms=TS,
                                           usage_source=UsageSource.PARTIAL_STREAM)
    assert partial.figure.low_nano == 0 and "partial stream" in partial.figure.note


def test_price_inference_carries_the_id() -> None:
    inf = make_inference(CASE1, inference_id="inf_x")
    p = kit.FakePricer().price_inference(inf, ts_ms=TS)
    assert p.inference_id == "inf_x" and p.figure.nano == 68_000_000


def test_resolve_min_cacheable_supports_tokenizer() -> None:
    fp = kit.FakePricer()
    opus = make_ctx("claude-opus-5-5")
    assert fp.min_cacheable_tokens(opus, ts_ms=TS) == 512
    assert fp.min_cacheable_tokens(make_ctx("claude-haiku-4-5"), ts_ms=TS) == 4096
    assert fp.min_cacheable_tokens(make_ctx("nope"), ts_ms=TS) is None
    assert fp.supports(opus, "per_message_effort", ts_ms=TS)
    assert not fp.supports(make_ctx("claude-sonnet-5"), "fast_mode", ts_ms=TS)
    assert not fp.supports(make_ctx("nope"), "batch", ts_ms=TS)
    assert fp.tokenizer_family(make_ctx("claude-sonnet-4-6"), ts_ms=TS) == "claude-legacy"
    assert fp.tokenizer_family(make_ctx("nope"), ts_ms=TS) is None
    assert fp.unit_rates(make_ctx("nope"), ts_ms=TS) is None
    unit = fp.unit_rates(opus, ts_ms=TS)
    assert unit is not None and unit.bucket_nano("cache_read", 100_000) == 20_000_000
    assert unit.web_search_nano == 10_000_000
    assert fp.resolve(opus, ts_ms=TS).min_cacheable_tokens == 512  # type: ignore[union-attr]


def test_contract_overrides_channels_and_dates() -> None:
    overrides = (("claude-opus-5-5", (("input", Decimal("3")), ("web_search", Decimal("0.005")))),)
    card = kit.FakePricer().with_contract(contract("0.5", overrides=overrides))
    p = card.price_usage(UsageBuckets(uncached_input=1_000_000, output=1_000_000,
                                      web_search_requests=2), make_ctx("claude-opus-5-5"),
                         ts_ms=TS)
    lines = {ln.bucket: ln.amount_nano for ln in p.lines}
    assert lines == {"uncached_input": 3_000_000_000, "output": 10_000_000_000,
                     "web_search": 10_000_000}
    scoped = kit.FakePricer().with_contract(contract(channels=("bedrock",)))
    listed = scoped.price_usage(CASE1, make_ctx("claude-opus-5-5"), ts_ms=TS)
    assert listed.figure.basis is Basis.LIST and listed.figure.nano == 68_000_000
    later = kit.FakePricer().with_contract(contract(effective_from="2026-10-01"))
    assert later.price_usage(CASE1, make_ctx("claude-opus-5-5"), ts_ms=TS).figure.nano \
        == 68_000_000
    ended = kit.FakePricer().with_contract(contract(effective_to="2026-09-01"))
    assert ended.price_usage(CASE1, make_ctx("claude-opus-5-5"), ts_ms=TS).figure.basis \
        is Basis.LIST
    bad = kit.FakePricer().with_contract(
        contract(overrides=(("claude-opus-5-5", (("tokens", Decimal("1")),)),)))
    with pytest.raises(PricingError):
        bad.price_usage(CASE1, make_ctx("claude-opus-5-5"), ts_ms=TS)
    with pytest.raises(UsageError):
        kit.FakePricer().with_contract("0.85")  # type: ignore[arg-type]


def test_missing_bucket_rates_fall_back() -> None:
    # Anthropic rows have no other-TTL write rate; OpenAI rows have no 5m/1h rates.
    other = price(UsageBuckets(cache_write_other=1_000_000, cache_write_other_ttl_s=1800))
    assert other.figure.nano == 5_000_000_000
    five = price(UsageBuckets(cache_write_5m=100_000), "gpt-5.6-sol", TS_0910)
    assert five.figure.nano == 500_000_000
    band = price(UsageBuckets(cache_write_5m=1_000_000), "gpt-5.6-sol", TS_0910)
    assert band.figure.nano == 10_000_000_000  # > 272K: the whole request at band rates


def test_generation_must_be_numeric() -> None:
    with pytest.raises(PricingError):
        kit._generation("5.x")


def test_rate_card_sha_is_deterministic() -> None:
    assert kit.FakePricer().rate_card_sha256 == kit.FakePricer().rate_card_sha256
    assert len(kit.FakePricer().rate_card_sha256) == 64


def test_fake_price_total_ranges_and_unpriced() -> None:
    fp = kit.FakePricer()
    items = [
        (make_inference(UsageBuckets(uncached_input=1000, cache_write_unknown=1_000_000)), TS),
        (make_inference(UsageBuckets(output=100), model="claude-foo-9"), TS),
        (make_inference(UsageBuckets(output=100), inference_id="inf_skip", billable=False), TS),
        (make_inference(UsageBuckets(output=7), inference_id="inf_ph",
                        usage_source=UsageSource.MESSAGE_START_ONLY, output_upper=10,
                        billing_path="subscription"), TS),
    ]
    total = kit.fake_price_total(fp, items)
    assert total.exact.nano == 4_000_000
    assert total.estimated is not None and total.estimated.low_nano == 5_000_000_000
    assert (total.priced_inferences, total.unpriced_inferences, total.unpriced_tokens) == (
        2, 1, 100)
    assert total.allowance is not None and total.allowance.evidence is Evidence.ESTIMATED
    assert total.coverage == str(Decimal(1_001_007) / Decimal(1_001_107))[:len(total.coverage)]
    empty = kit.fake_price_total(fp, [])
    assert empty.coverage == "1" and empty.exact.nano == 0 and empty.allowance is None


# ---------- hypothesis: unit rates agree with price_usage ----------

_MODELS = ["claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-fable-5",
           "claude-fable-5-1", "claude-mythos-5-1", "claude-sonnet-5", "claude-sonnet-4-6",
           "claude-haiku-4-5"]


@settings(max_examples=150, deadline=None)
@given(model=st.sampled_from(_MODELS),
       tier=st.sampled_from(["standard", "batch"]),
       geo=st.sampled_from([None, "us"]),
       speed=st.sampled_from(["standard", "fast"]),
       u=st.integers(0, 10**7), r=st.integers(0, 10**7), w5=st.integers(0, 10**7),
       w1=st.integers(0, 10**7), wu=st.integers(0, 10**7), o=st.integers(0, 10**6),
       web=st.integers(0, 50))
def test_unit_rates_agree_with_price_usage(model, tier, geo, speed, u, r, w5, w1, wu, o,
                                           web) -> None:
    fp = kit.FakePricer()
    ctx = make_ctx(model, service_tier=tier, inference_geo=geo, speed=speed)
    usage = UsageBuckets(uncached_input=u, cache_read=r, cache_write_5m=w5, cache_write_1h=w1,
                         cache_write_unknown=wu, output=o, web_search_requests=web)
    p = fp.price_usage(usage, ctx, ts_ms=TS)
    unit = fp.unit_rates(ctx, ts_ms=TS)
    assert unit is not None and p.figure.nano is not None
    assert sum(unit.bucket_nano(ln.bucket, ln.quantity) for ln in p.lines) == p.figure.nano
    assert p.figure.nano == p.exact_nano + (p.estimated.nano if p.estimated else 0)

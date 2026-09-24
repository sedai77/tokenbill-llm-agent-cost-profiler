"""FakePricer on GitHub Copilot rows (CORE-AMENDMENTS K-5; addendum §6.2, Appendix C.G1–G17; F-KIT-C
acceptance): every golden case to the nano with its label, pool totals, and Anthropic / OpenAI
behaviour unchanged."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core import testing as kit
from tokenbill.core.builders import make_copilot_ctx, make_inference
from tokenbill.core.facts import load as load_facts
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.money import credits_str_to_nano, nano_aiu_to_nano, usd_str_to_nano
from tokenbill.core.records import PricingContext, UsageBuckets
from tokenbill.core.types import ContractOverlay

P = kit.FakePricer()
DAY = "2026-09-23"


def price(usage: UsageBuckets, model: str = "claude-opus-5-5", day: str = DAY, **kw):
    billable = kw.pop("billable", True)
    return P.price_usage(usage, make_copilot_ctx(model, **kw), ts_ms=kit._ts(day),
                         billable=billable)


def rng(p) -> tuple[int | None, int | None, int | None]:
    return (p.figure.nano, p.figure.low_nano, p.figure.high_nano)


G1 = UsageBuckets(uncached_input=12_000, cache_read=180_000, cache_write_unknown=6000,
                  output=3000)


def test_g1_opus_5_5_unknown_ttl_writes() -> None:
    p = price(G1)
    assert rng(p) == (174_000_000, 174_000_000, 192_000_000)
    assert p.figure.evidence is Evidence.ESTIMATED and p.figure.basis is Basis.LIST_EQUIVALENT
    assert p.exact_nano == 144_000_000 and not p.figure.is_billed_eligible
    write = next(ln for ln in p.lines if ln.bucket == "cache_write_unknown")
    assert (write.amount_nano, write.low_nano, write.high_nano) == (30_000_000, 30_000_000,
                                                                     48_000_000)
    assert price(G1, write_ttl_hint="1h").figure.nano == 192_000_000
    assert price(G1, write_ttl_hint="5m").figure.nano == 174_000_000


def test_g2_to_g4_modifiers() -> None:
    assert rng(price(G1, routing="auto")) == (156_600_000, 156_600_000, 172_800_000)
    assert "github.auto" in price(G1, routing="auto").lines[0].modifier_ids
    g3 = price(G1, compliance="data_residency")
    assert (g3.figure.nano, g3.figure.high_nano) == (191_400_000, 211_200_000)
    assert price(G1, compliance="fedramp").figure.nano == 191_400_000
    g4 = price(G1, routing="auto", compliance="data_residency")
    assert (g4.figure.nano, g4.figure.high_nano) == (172_260_000, 190_080_000)
    assert set(g4.lines[0].modifier_ids) == {"github.auto", "github.compliance"}
    assert price(G1, routing="unknown").figure.nano == 174_000_000  # only "auto" discounts


def test_g5_long_context_band_hypothesis_a() -> None:
    big = UsageBuckets(uncached_input=20_000, cache_read=280_000, output=4000)
    small = UsageBuckets(uncached_input=20_000, cache_read=250_000, output=4000)
    for tier in (None, "long_context"):
        g5 = price(big, "gpt-5.5", "2026-09-10", context_tier=tier)
        assert g5.figure.nano == 660_000_000 and g5.figure.evidence is Evidence.EXACT
    for tier in (None, "default"):
        g5s = price(small, "gpt-5.5", "2026-09-10", context_tier=tier)
        assert g5s.figure.nano == 345_000_000 and g5s.figure.evidence is Evidence.EXACT


def test_g5b_band_hypotheses_disagree() -> None:
    small = UsageBuckets(uncached_input=20_000, cache_read=250_000, output=4000)
    g5b = price(small, "gpt-5.5", "2026-09-10", context_tier="long_context")
    assert rng(g5b) == (345_000_000, 345_000_000, 630_000_000)
    assert g5b.figure.evidence is Evidence.ESTIMATED and kit.DQ_COPILOT_BAND_HYPOTHESIS in (
        g5b.figure.note)
    assert all(not ln.exact for ln in g5b.lines) and g5b.exact_nano == 0
    big = UsageBuckets(uncached_input=20_000, cache_read=280_000, output=4000)
    down = price(big, "gpt-5.5", "2026-09-10", context_tier="default")  # A band, B default
    assert rng(down) == (660_000_000, 360_000_000, 660_000_000)


def test_g6_g7_gpt_5_6_sol_dated_rows() -> None:
    usage = UsageBuckets(uncached_input=2000, cache_read=6000, cache_write_unknown=2000,
                         output=1000)
    g6 = price(usage, "gpt-5.6-sol", "2026-09-10")
    assert rng(g6) == (40_400_000, 40_400_000, 40_400_000)  # zero-width write range
    assert g6.figure.evidence is Evidence.ESTIMATED
    assert price(usage, "gpt-5.6-sol", "2026-08-25").figure.nano == 20_200_000
    assert price(usage, "gpt-5.6-sol", "2026-08-20").figure.nano == 27_750_000


def test_g8_fast_mode_premium() -> None:
    usage = UsageBuckets(uncached_input=10_000, cache_read=90_000, output=2000)
    fast = price(usage, "claude-opus-4-8", speed="fast")
    std = price(usage, "claude-opus-4-8")
    assert (fast.figure.nano, std.figure.nano) == (290_000_000, 145_000_000)
    assert fast.figure.evidence is Evidence.EXACT and std.figure.evidence is Evidence.EXACT
    assert fast.figure.nano - std.figure.nano == 145_000_000
    assert "github.fast.opus-4-8" in fast.lines[0].modifier_ids
    g8b = dataclasses.replace(usage, cache_write_unknown=5000)
    fb, sb = rng(price(g8b, "claude-opus-4-8", speed="fast")), rng(price(g8b, "claude-opus-4-8"))
    assert tuple(a - b for a, b in zip(fb, sb, strict=True)) == (176_250_000, 176_250_000,
                                                                  195_000_000)
    assert price(usage, "claude-opus-5", speed="fast").figure.nano == 145_000_000  # no fast row


def test_g9_promotion_and_expiry() -> None:
    usage = UsageBuckets(uncached_input=100_000, cache_read=400_000, output=10_000)
    assert price(usage, "gemini-3.8-flash").figure.nano == 142_500_000
    expired = price(usage, "gemini-3.8-flash", "2027-01-01")
    assert expired.figure.nano is None and expired.unpriced_reason == "promotion expired"
    assert kit.UNPRICED_DQ[expired.unpriced_reason] == "dq.promotion_expired"


def test_g10_g14_other_vendors() -> None:
    g10 = price(UsageBuckets(uncached_input=50_000, cache_read=160_000, output=5000), "grok-4.7")
    assert g10.figure.nano == 420_000_000 and g10.figure.evidence is Evidence.EXACT
    assert price(UsageBuckets(cache_read=1_000_000), "kimi-k2.7-code").figure.nano == 190_000_000


def test_g11_nano_aiu_parity() -> None:
    usage = UsageBuckets(uncached_input=6, cache_read=127_386, cache_write_unknown=2220,
                         output=6210)
    p = price(usage, "claude-opus-4-7")
    assert p.figure.nano == 232_848_000  # the write billed at the published price
    assert nano_aiu_to_nano(23_284_800_000) == (232_848_000, Decimal(0))


def test_g12_utility_call_is_exact_zero() -> None:
    for model in ("gpt-4o-mini", "claude-opus-5-5"):
        p = price(UsageBuckets(uncached_input=900, output=40), model, billable=False)
        assert p.figure.nano == 0 and p.figure.evidence is Evidence.EXACT
        assert p.unpriced_reason is None
    assert price(UsageBuckets(output=1), "gpt-4o-mini").figure.nano is None  # billable: no row


def test_g13_both_copilot_paths_price_into_the_pool() -> None:
    items = []
    for i, path in enumerate(("copilot_pool", "copilot_direct")):
        inf = make_inference(G1, model="claude-opus-5-5", provider="github",
                             channel="github_copilot", billing_path=path,
                             inference_id=f"inf-{i}")
        items.append((inf, kit._ts(DAY)))
    anthropic = make_inference(UsageBuckets(uncached_input=1000), inference_id="inf-a")
    sub = make_inference(UsageBuckets(uncached_input=1000), billing_path="subscription",
                         inference_id="inf-s")
    total = kit.fake_price_total(P, [*items, (anthropic, kit._ts(DAY)), (sub, kit._ts(DAY))])
    assert total.pool is not None and total.pool.basis is Basis.LIST_EQUIVALENT
    assert (total.pool.nano, total.pool.low_nano, total.pool.high_nano) == (
        348_000_000, 348_000_000, 384_000_000)
    assert total.pool.evidence is Evidence.ESTIMATED
    assert total.allowance is not None and total.allowance.nano == 4_000_000  # subscription only
    assert total.exact.nano == 4_000_000 and total.exact.basis is Basis.LIST  # unchanged
    only_claude = kit.fake_price_total(P, [(anthropic, kit._ts(DAY))])
    assert only_claude.pool is None and only_claude.allowance is None
    exact_pool = kit.fake_price_total(P, [(dataclasses.replace(
        items[0][0], usage=UsageBuckets(output=1000)), kit._ts(DAY))])
    assert exact_pool.pool is not None and exact_pool.pool.evidence is Evidence.EXACT


def test_g15_writes_folded_to_input() -> None:
    g15 = price(UsageBuckets(cache_write_unknown=1000), "gpt-5.3-codex")
    assert rng(g15) == (1_750_000, 1_750_000, 1_750_000)
    assert g15.figure.evidence is Evidence.ESTIMATED
    assert kit.DQ_COPILOT_WRITE_FOLDED in g15.figure.note
    for bucket in ("cache_write_5m", "cache_write_1h"):
        p = price(UsageBuckets(**{bucket: 1000}), "gpt-5.3-codex")
        assert rng(p) == (1_750_000, 1_750_000, 1_750_000) and not p.lines[0].exact
    other = price(UsageBuckets(cache_write_other=1000, cache_write_other_ttl_s=300),
                  "gemini-3.5-flash")
    assert other.figure.nano == 1_500_000 and other.figure.evidence is Evidence.ESTIMATED
    # rows with a write price keep exact writes
    exact_write = price(UsageBuckets(cache_write_5m=1000), "gpt-5.6-sol", "2026-09-10")
    assert exact_write.figure.evidence is Evidence.EXACT and exact_write.figure.nano == 5_000_000


def test_g16_before_the_effective_date() -> None:
    p = price(G1, day="2026-09-21")
    assert p.figure.nano is None and p.unpriced_reason == "model before effective date"
    assert kit.UNPRICED_DQ[p.unpriced_reason] == "dq.model_before_effective_date"


def test_g17_credit_and_usd_strings() -> None:
    assert credits_str_to_nano("42.726213") == (427_262_130, Decimal(0))
    nano, rem = usd_str_to_nano("0.4272621300000001")
    assert nano == 427_262_130 and rem == Decimal("1E-16")


def test_copilot_rows_never_fall_back_or_take_contracts() -> None:
    assert price(UsageBuckets(output=1), "claude-mythos-5-1").figure.nano is None  # no Copilot row
    overlay = ContractOverlay(name="half", multiplier=Decimal("0.5"), overrides=(),
                              effective_from="2026-01-01", effective_to=None, derived=False,
                              assumed_fields=())
    card = P.with_contract(overlay)
    for path in ("copilot_pool", "copilot_direct"):
        p = card.price_usage(G1, make_copilot_ctx(billing_path=path), ts_ms=kit._ts(DAY))
        assert p.figure.nano == 174_000_000 and p.figure.basis is Basis.LIST_EQUIVALENT
        assert all(ln.layer == "builtin" for ln in p.lines)
    odd = card.price_usage(G1, make_copilot_ctx(billing_path="unknown"), ts_ms=kit._ts(DAY))
    assert odd.figure.nano == 174_000_000 and odd.figure.basis is Basis.LIST
    anthropic = PricingContext(provider="anthropic", channel="anthropic_api",
                               model="claude-opus-5-5", model_raw="claude-opus-5-5")
    assert card.price_usage(UsageBuckets(output=1000), anthropic,
                            ts_ms=kit._ts(DAY)).figure.basis is Basis.CONTRACT


def test_anthropic_and_openai_prices_unchanged() -> None:
    opus = PricingContext(provider="anthropic", channel="anthropic_api", model="claude-opus-5-5",
                          model_raw="claude-opus-5-5")
    case1 = UsageBuckets(uncached_input=1000, cache_read=100_000, cache_write_5m=2000,
                         cache_write_1h=3000, output=500)
    assert P.price_usage(case1, opus, ts_ms=kit._ts(DAY)).figure.nano == 68_000_000
    sol = PricingContext(provider="openai", channel="openai_api", model="gpt-5.6-sol",
                         model_raw="gpt-5.6-sol")
    big = UsageBuckets(uncached_input=300_000, output=1000)
    assert P.price_usage(big, sol, ts_ms=kit._ts("2026-09-10")).figure.nano == 2_430_000_000
    # context_tier and routing only act on the Copilot channel
    tiered = dataclasses.replace(sol, context_tier="default", routing="auto")
    assert P.price_usage(big, tiered, ts_ms=kit._ts("2026-09-10")).figure.nano == 2_430_000_000


def test_resolution_helpers_on_copilot_rows() -> None:
    ctx = make_copilot_ctx("claude-opus-4-8")
    ts = kit._ts(DAY)
    assert P.supports(ctx, "fast_mode", ts_ms=ts)
    assert not P.supports(make_copilot_ctx(), "fast_mode", ts_ms=ts)
    assert P.tokenizer_family(ctx, ts_ms=ts) == "claude-4.7+"
    assert P.min_cacheable_tokens(ctx, ts_ms=ts) is None
    rates = P.resolve(make_copilot_ctx("gpt-5.3-codex"), ts_ms=ts)
    assert rates is not None and rates.cache_write_5m is None
    unit = P.unit_rates(make_copilot_ctx("gpt-5.3-codex"), ts_ms=ts)
    assert unit is not None and unit.bucket_nano("cache_write_unknown", 1000) == 1_750_000


def test_every_copilot_row_prices_and_matches_unit_rates() -> None:
    for row in load_facts().copilot.rate_rows:
        if not row.enabled:
            continue
        ctx = make_copilot_ctx(row.model)
        ts = kit._ts(row.effective_from)
        rates = P.resolve(ctx, ts_ms=ts)
        assert rates is not None and rates.row_id == row.row_id, row.row_id
        assert rates.input == row.input_usd_per_mtok
        usage = UsageBuckets(uncached_input=1234, cache_read=5678, output=91)
        p = P.price_usage(usage, ctx, ts_ms=ts)
        unit = P.unit_rates(ctx, ts_ms=ts)
        assert unit is not None
        assert sum(unit.bucket_nano(ln.bucket, ln.quantity) for ln in p.lines) == p.figure.nano


@settings(max_examples=150, deadline=None)
@given(st.integers(0, 400_000), st.integers(0, 400_000), st.integers(0, 50_000),
       st.integers(0, 30_000), st.sampled_from([None, "default", "long_context"]),
       st.sampled_from(["gpt-5.5", "gpt-5.6-sol", "grok-4.7", "claude-opus-5-5",
                        "gpt-5.3-codex"]))
def test_band_hypothesis_property(uncached: int, read: int, write: int, out: int,
                                  tier: str | None, model: str) -> None:
    usage = UsageBuckets(uncached_input=uncached, cache_read=read, cache_write_unknown=write,
                         output=out)
    p = price(usage, model, "2026-09-10" if model != "claude-opus-5-5" else DAY,
              context_tier=tier)
    a = price(usage, model, "2026-09-10" if model != "claude-opus-5-5" else DAY)
    assert p.figure.nano == a.figure.nano  # the point is always hypothesis A
    if p.figure.low_nano is not None:
        assert p.figure.low_nano <= p.figure.nano <= p.figure.high_nano
    assert p.figure.basis is Basis.LIST_EQUIVALENT


def test_pricer_conformance_includes_the_copilot_goldens() -> None:
    summary = kit.assert_pricer_conforms(P, samples=20)
    assert {"C.G1", "C.G5b", "C.G8b", "C.G9", "C.G16"} <= set(summary["golden_cases"])
    assert "1" in summary["golden_cases"]


def test_rate_card_hash_covers_the_copilot_rows() -> None:
    assert P.rate_card_sha256 == kit.FakePricer().rate_card_sha256
    assert len(P.rate_card_sha256) == 64


@pytest.mark.parametrize("model,day", [("claude-opus-4-5", "2026-09-05"),
                                       ("gpt-5.6-luna", "2026-07-01")])
def test_closed_and_future_rows(model: str, day: str) -> None:
    assert price(UsageBuckets(output=1), model, day).figure.nano is None

"""SPEC §6.9 golden billing corpus: all 24 cases, hand-computed, compared in int nano.

Rates (USD per MTok, SPEC §19.1): Opus 5.5 $4 in / $20 out, reads 0.05× ($0.20), 5m writes 1.25×
($5), 1h writes 2× ($8); Opus 5 and 4.8 $5 / $25; Fable 5 / 5.1 $10 / $50 with reads 0.1× / 0.025×;
Haiku 4.5 $1 / $5, reads $0.10. Line amount = tokens × rate / 10^6 USD, rounded half-even once.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from tokenbill.core.builders import make_inference
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.models import normalize_model
from tokenbill.core.records import InferenceKind, UsageBuckets, UsageSource
from tokenbill.rates import billing_rules
from tokenbill.rates.contract import make_overlay
from tokenbill.rates.engine import UNPRICED_REASONS, RateCard, price_total

from .helpers import builtin, card, ctx, nano, ts

DAY = "2026-09-23"
CASE1 = UsageBuckets(uncached_input=1000, cache_read=100_000, cache_write_5m=2000,
                     cache_write_1h=3000, output=500)


def _lines(priced: object) -> dict[str, tuple[int, int | None, int | None, bool]]:
    return {ln.bucket: (ln.amount_nano, ln.low_nano, ln.high_nano, ln.exact)
            for ln in priced.lines}  # type: ignore[attr-defined]


def test_case_1_opus_5_5_standard() -> None:
    # 1,000 × $4 = 0.004; 100,000 × $0.20 = 0.020; 2,000 × $5 = 0.010; 3,000 × $8 = 0.024;
    # 500 × $20 = 0.010 → 0.068
    p = card().price_usage(CASE1, ctx(), ts_ms=ts(DAY))
    assert p.figure.nano == nano("0.068") and p.figure.evidence is Evidence.EXACT
    assert p.figure.basis is Basis.LIST and p.figure.is_billed_eligible
    assert _lines(p) == {"uncached_input": (4_000_000, None, None, True),
                         "cache_read": (20_000_000, None, None, True),
                         "cache_write_5m": (10_000_000, None, None, True),
                         "cache_write_1h": (24_000_000, None, None, True),
                         "output": (10_000_000, None, None, True)}
    assert p.exact_nano == nano("0.068") and p.estimated is None and p.unpriced_reason is None


def test_case_2_us_geo() -> None:
    # 0.068 × 1.1 = 0.0748 (every bucket ×1.1)
    p = card().price_usage(CASE1, ctx(inference_geo="us"), ts_ms=ts(DAY))
    assert p.figure.nano == nano("0.0748")
    assert "anthropic.inference_geo.us" in p.lines[0].modifier_ids


@pytest.mark.parametrize(("extra", "usd"), [({"service_tier": "batch"}, "0.034"),
                                            ({"service_tier": "batch", "inference_geo": "us"},
                                             "0.0374")])
def test_case_3_batch_and_batch_plus_geo(extra: dict[str, str], usd: str) -> None:
    # 0.068 × 0.5 = 0.034; × 0.5 × 1.1 = 0.0374
    assert card().price_usage(CASE1, ctx(**extra), ts_ms=ts(DAY)).figure.nano == nano(usd)


def test_case_4_fast_mode() -> None:
    # base $8/$40: 1,000 × 8 = 0.008; 100,000 × 0.40 = 0.040; 2,000 × 10 = 0.020;
    # 3,000 × 16 = 0.048; 500 × 40 = 0.020 → 0.136
    p = card().price_usage(CASE1, ctx(speed="fast"), ts_ms=ts(DAY))
    assert p.figure.nano == nano("0.136")
    assert "anthropic.fast.opus-5-5" in p.lines[0].modifier_ids


def test_case_5_fable_5_1_reads_vs_fable_5() -> None:
    # 1,000,000 reads × $0.25 (0.025 × 10) = 0.25 vs × $1.00 = 1.00, both dated 2026-09-10
    reads = UsageBuckets(cache_read=1_000_000)
    c = card()
    assert c.price_usage(reads, ctx("claude-fable-5-1"), ts_ms=ts("2026-09-10")).figure.nano \
        == nano("0.25")
    assert c.price_usage(reads, ctx("claude-fable-5"), ts_ms=ts("2026-09-10")).figure.nano \
        == nano("1.00")


def test_case_6_web_search() -> None:
    # 3 × $0.01 = 0.03 on top of case 1
    p = card().price_usage(dataclasses.replace(CASE1, web_search_requests=3), ctx(),
                           ts_ms=ts(DAY))
    assert p.figure.nano == nano("0.068") + nano("0.03")
    web = [ln for ln in p.lines if ln.bucket == "web_search"]
    assert len(web) == 1 and web[0].quantity == 3 and web[0].unit_usd_per_mtok == "0.01"


def test_case_7_bedrock_scope() -> None:
    # 1,000,000 × $5.00 global; × $5.50 in-region (+10%); unknown scope → range [5.00, 5.50]
    inp = UsageBuckets(uncached_input=1_000_000)
    base = ctx("claude-opus-5", channel="bedrock", billing_path="bedrock")
    c = card()
    g = c.price_usage(inp, dataclasses.replace(base, endpoint_scope="global"), ts_ms=ts(DAY))
    r = c.price_usage(inp, dataclasses.replace(base, endpoint_scope="regional"), ts_ms=ts(DAY))
    u = c.price_usage(inp, dataclasses.replace(base, endpoint_scope="unknown"), ts_ms=ts(DAY))
    assert g.figure.nano == nano("5.00") and g.figure.evidence is Evidence.EXACT
    assert r.figure.nano == nano("5.50") and r.figure.evidence is Evidence.EXACT
    assert u.figure.evidence is Evidence.ESTIMATED
    assert (u.figure.nano, u.figure.low_nano, u.figure.high_nano) == \
        (nano("5.00"), nano("5.00"), nano("5.50"))
    # per-line split: the only line is a range, so nothing is exact
    assert u.exact_nano == 0 and u.estimated is not None
    assert _lines(u) == {"uncached_input": (nano("5.00"), nano("5.00"), nano("5.50"), False)}
    assert "dq.scope_unknown" in u.figure.note


def test_case_8_contract_multiplier() -> None:
    # 0.068 × 0.85 = 0.0578 (0.0034 + 0.017 + 0.0085 + 0.0204 + 0.0085), basis CONTRACT
    overlay = make_overlay(name="acme", multiplier=Decimal("0.85"), overrides={},
                           effective_from="2026-01-01")
    p = RateCard([builtin()], contract=overlay).price_usage(CASE1, ctx(), ts_ms=ts(DAY))
    assert p.figure.nano == nano("0.0578") and p.figure.basis is Basis.CONTRACT
    assert p.figure.is_billed_eligible and p.lines[0].layer == "contract"


def test_case_9_unknown_ttl_writes() -> None:
    # exact: 1,000 × 4 + 500 × 20 = 0.014; writes 1,000,000 × [5, 8] → [5.00, 8.00], width 3.00
    usage = UsageBuckets(uncached_input=1000, cache_write_unknown=1_000_000, output=500)
    p = card().price_usage(usage, ctx(), ts_ms=ts(DAY))
    assert p.exact_nano == nano("0.014")
    assert p.estimated is not None and p.estimated.evidence is Evidence.ESTIMATED
    assert (p.estimated.low_nano, p.estimated.high_nano) == (nano("5.00"), nano("8.00"))
    assert p.estimated.high_nano - p.estimated.low_nano == nano("3.00")
    assert p.estimated.nano == nano("5.00")                 # point: the 5m rate without a hint
    assert _lines(p)["uncached_input"][3] and _lines(p)["output"][3]
    assert not _lines(p)["cache_write_unknown"][3]
    assert p.figure.evidence is Evidence.ESTIMATED and p.figure.nano == nano("5.014")


@pytest.mark.parametrize(("scope", "high"), [("global", "0.0404"), ("unknown", "0.04444")])
def test_case_10_gpt_5_6_sol(scope: str, high: str) -> None:
    # 2,000 × 4 = 0.008; 6,000 × 0.40 = 0.0024; 2,000 × 5 = 0.010; 1,000 × 20 = 0.020 → 0.0404;
    # an unknown endpoint scope ranges to regional processing (+10%): 0.04444
    usage = UsageBuckets(uncached_input=2000, cache_read=6000, cache_write_other=2000,
                         cache_write_other_ttl_s=1800, output=1000)
    p = card().price_usage(usage, ctx("gpt-5.6-sol", endpoint_scope=scope),
                           ts_ms=ts("2026-09-10"))
    assert p.figure.nano == nano("0.0404")
    assert (p.figure.high_nano if p.figure.high_nano is not None else p.figure.nano) == nano(high)
    assert (p.figure.evidence is Evidence.EXACT) == (scope == "global")


def test_case_11_long_context_band() -> None:
    # 300,000 > 272,000: the whole request at the band: 300,000 × 8 + 1,000 × 30 = 2.43
    usage = UsageBuckets(uncached_input=300_000, output=1000)
    c = card()
    sol = ctx("gpt-5.6-sol", endpoint_scope="global")
    p = c.price_usage(usage, sol, ts_ms=ts("2026-09-10"))
    assert p.figure.nano == nano("2.43")
    assert {ln.unit_usd_per_mtok for ln in p.lines} == {"8", "30"}
    # 272,000 exactly is not above the threshold: base rates
    at = c.price_usage(UsageBuckets(uncached_input=272_000), sol, ts_ms=ts("2026-09-10"))
    assert at.figure.nano == 272_000 * 4_000
    # resolve() without usage gives the base rates
    rates = c.resolve(sol, ts_ms=ts("2026-09-10"))
    assert rates is not None and rates.input == Decimal("4.00") and not rates.long_context_band


def test_case_12_declined_attempt_and_fallback() -> None:
    # declined attempt with 0 output on Fable 5: not billed (anthropic.refusal.pre_output), EXACT 0;
    # the fallback on Opus 4.8: 1,000 × 5 + 100 × 25 = 0.0075
    rule = billing_rules.refusal_rule(0)
    assert rule.rule_id == "anthropic.refusal.pre_output" and rule.billable is False
    declined = make_inference({"uncached_input": 1000}, model="claude-fable-5",
                              kind=InferenceKind.FALLBACK_DECLINED, billable=rule.billable,
                              billing_rule_id=rule.rule_id)
    fallback = make_inference({"uncached_input": 1000, "output": 100}, model="claude-opus-4-8",
                              kind=InferenceKind.FALLBACK)
    c = card()
    p = c.price_inference(declined, ts_ms=ts(DAY))
    assert p.figure.nano == 0 and p.figure.evidence is Evidence.EXACT
    assert all(ln.amount_nano == 0 and ln.exact for ln in p.lines) and p.lines
    f = c.price_inference(fallback, ts_ms=ts(DAY))
    assert f.figure.nano == nano("0.0075") and f.lines[0].rate_row_id.endswith(
        "claude-opus-4-8/2026-05-28")
    total = price_total(c, [(declined, ts(DAY)), (fallback, ts(DAY))])
    assert total.exact.nano == nano("0.0075") and total.priced_inferences == 1


def test_case_13_ambiguous_decline() -> None:
    # 6 output tokens on Fable 5: anthropic.refusal.ambiguous → range [0, full], ESTIMATED;
    # full = 2,000 × 10 + 6 × 50 = 0.0203
    rule = billing_rules.refusal_rule(6)
    assert rule.rule_id == "anthropic.refusal.ambiguous" and rule.billable is None
    inf = make_inference({"uncached_input": 2000, "output": 6}, model="claude-fable-5",
                         kind=InferenceKind.FALLBACK_DECLINED, billable=rule.billable,
                         billing_rule_id=rule.rule_id)
    p = card().price_inference(inf, ts_ms=ts(DAY))
    assert p.figure.evidence is Evidence.ESTIMATED
    assert (p.figure.nano, p.figure.low_nano, p.figure.high_nano) == \
        (nano("0.0203"), 0, nano("0.0203"))
    assert p.exact_nano == 0
    assert billing_rules.refusal_rule(17).rule_id == "anthropic.refusal.mid_stream"


def test_case_14_compaction_plus_message_iteration() -> None:
    # both iterations at the message model (Opus 5.5): compaction 50,000 × 4 + 2,000 × 20 = 0.24;
    # message 1,000 × 4 + 20,000 × 0.20 + 300 × 20 = 0.014 → 0.254
    compaction = make_inference({"uncached_input": 50_000, "output": 2000},
                                kind=InferenceKind.COMPACTION, inference_id="it-0")
    message = make_inference({"uncached_input": 1000, "cache_read": 20_000, "output": 300},
                             inference_id="it-1")
    total = price_total(card(), [(compaction, ts(DAY)), (message, ts(DAY))])
    assert total.exact.nano == nano("0.254") and total.estimated is None
    assert total.priced_inferences == 2 and total.coverage == "1"


def test_case_15_unknown_model() -> None:
    p = card().price_usage(UsageBuckets(output=1), ctx("claude-foo-9"), ts_ms=ts(DAY))
    assert p.figure.nano is None and p.unpriced_reason == "no rate row"
    assert p.lines == () and p.exact_nano == 0 and p.estimated is None
    assert p.figure.note == "unpriced: no rate row"
    unknown = make_inference({"uncached_input": 3000}, model="claude-foo-9")
    known = make_inference({"uncached_input": 1000})
    total = price_total(card(), [(unknown, ts(DAY)), (known, ts(DAY))])
    assert total.unpriced_inferences == 1 and total.unpriced_tokens == 3000
    assert Decimal(total.coverage) == Decimal("0.25") < 1


def test_case_16_model_ids_resolve_to_base_rows() -> None:
    c = card()
    # dated snapshot id → base row, first party
    mid = normalize_model("claude-haiku-4-5-20251001")
    r = c.resolve(ctx(mid.model), ts_ms=ts(DAY))
    assert r is not None and r.row_id == "anthropic/anthropic_api/claude-haiku-4-5/2025-10-15"
    # Vertex id → vertex channel, scope unknown → global point with the regional range
    mid = normalize_model("claude-sonnet-4-6@20260101")
    assert (mid.model, mid.channel_hint, mid.endpoint_scope) == \
        ("claude-sonnet-4-6", "vertex", "unknown")
    r = c.resolve(ctx(mid.model, channel="vertex", endpoint_scope=mid.endpoint_scope),
                  ts_ms=ts(DAY))
    assert r is not None and r.channel == "vertex" and r.input == Decimal("3.00")
    assert r.scope_range is not None and r.scope_range.input == Decimal("3.300")
    # [1m] suffix → base model
    assert normalize_model("claude-opus-5-5[1m]").model == "claude-opus-5-5"
    # Bedrock global profile → bedrock, scope global (no range)
    mid = normalize_model("global.anthropic.claude-opus-5-5-v1:0")
    assert (mid.model, mid.channel_hint, mid.endpoint_scope) == \
        ("claude-opus-5-5", "bedrock", "global")
    r = c.resolve(ctx(mid.model, channel="bedrock", endpoint_scope="global"), ts_ms=ts(DAY))
    assert r is not None and r.row_id.startswith("anthropic/bedrock/claude-opus-5-5/")
    assert r.scope_range is None and r.input == Decimal("4.00")


def test_case_17_before_effective_date() -> None:
    p = card().price_usage(CASE1, ctx(), ts_ms=ts("2026-09-21"))
    assert p.figure.nano is None and p.unpriced_reason == "model before effective date"
    assert UNPRICED_REASONS[p.unpriced_reason] == "dq.model_before_effective_date"


def test_case_18_one_hour_writes_batch_and_geo() -> None:
    # 1,000,000 × 4 × 2 × 1.1 × 0.5 = 4.40
    p = card().price_usage(UsageBuckets(cache_write_1h=1_000_000),
                           ctx(service_tier="batch", inference_geo="us"), ts_ms=ts(DAY))
    assert p.figure.nano == nano("4.40")


def test_case_19_contract_multiplier_rounds_half_even() -> None:
    # 3 × 25 × 0.8537 / 10^6 USD = 64,027.5 nano → 64,028 (half-even)
    overlay = make_overlay(name="c19", multiplier=Decimal("0.8537"), overrides={},
                           effective_from="2026-01-01")
    p = RateCard([builtin()], contract=overlay).price_usage(
        UsageBuckets(output=3), ctx("claude-opus-4-8"), ts_ms=ts(DAY))
    assert p.figure.nano == 64_028
    unit = RateCard([builtin()], contract=overlay).unit_rates(ctx("claude-opus-4-8"),
                                                              ts_ms=ts(DAY))
    # output 25 × 0.8537 = 21.3425 $/MTok alone needs s = 10; the 5m write 6.25 × 0.8537 =
    # 5.335625 needs s = 12, the smallest scale making every bucket integral
    assert unit is not None and unit.scale_exp == 12 and unit.bucket_nano("output", 3) == 64_028


def test_case_20_a_million_one_token_lines() -> None:
    # 1 Haiku 4.5 read token = 1 × 0.10 / 10^6 USD = 100 nano; 10^6 lines → 10^8 nano = $0.10
    c = card()
    haiku = ctx("claude-haiku-4-5")
    one = UsageBuckets(cache_read=1)
    assert c.price_usage(one, haiku, ts_ms=ts(DAY)).figure.nano == 100
    unit = c.unit_rates(haiku, ts_ms=ts(DAY))
    assert unit is not None
    assert sum(unit.bucket_nano("cache_read", 1) for _ in range(1_000_000)) == nano("0.10")
    items = [(make_inference({"cache_read": 1}, model="claude-haiku-4-5"), ts(DAY))] * 20_000
    assert price_total(c, items).exact.nano == 20_000 * 100


def test_case_21_launch_row_and_promotion_expiry() -> None:
    big = UsageBuckets(uncached_input=300_000, output=1000)
    c = card()
    launch = c.price_usage(big, ctx("gpt-5.6-sol"), ts_ms=ts("2026-08-01"))
    assert launch.figure.nano is None and launch.unpriced_reason == "unverified rate row"
    expired = c.price_usage(big, ctx("gpt-5.6-sol"), ts_ms=ts("2026-11-22"))
    assert expired.figure.nano is None and expired.unpriced_reason == "promotion expired"
    assert UNPRICED_REASONS["promotion expired"] == "dq.promotion_expired"
    # the last promotional day is still priced (effective_to 2026-11-22 is exclusive)
    assert c.price_usage(big, ctx("gpt-5.6-sol", endpoint_scope="global"),
                         ts_ms=ts("2026-11-21")).figure.nano == nano("2.43")


def test_case_22_fable_5_1_before_launch() -> None:
    p = card().price_usage(UsageBuckets(cache_read=1_000_000), ctx("claude-fable-5-1"),
                           ts_ms=ts("2026-08-20"))
    assert p.figure.nano is None and p.unpriced_reason == "model before effective date"


def test_case_23_subscription_is_list_equivalent() -> None:
    c = card()
    sub = ctx(billing_path="subscription")
    p = c.price_usage(CASE1, sub, ts_ms=ts(DAY))
    assert p.figure.nano == nano("0.068") and p.figure.basis is Basis.LIST_EQUIVALENT
    assert not p.figure.is_billed_eligible
    inf = make_inference(CASE1, ctx=sub)
    total = price_total(c, [(inf, ts(DAY))])
    assert total.exact.nano == 0 and total.allowance is not None
    assert total.allowance.nano == nano("0.068") and total.pool is None


def test_case_24_message_start_only_output() -> None:
    # exact: 100,000 reads × 0.20 = 0.020; output line [3, 403] × $20 → [0.00006, 0.00806],
    # point at the logged 3 tokens
    p = card().price_usage(UsageBuckets(cache_read=100_000, output=3), ctx(), ts_ms=ts(DAY),
                           usage_source=UsageSource.MESSAGE_START_ONLY, output_upper=403)
    assert p.exact_nano == nano("0.020")
    assert _lines(p)["output"] == (nano("0.00006"), nano("0.00006"), nano("0.00806"), False)
    assert _lines(p)["cache_read"] == (nano("0.020"), None, None, True)
    assert p.estimated is not None and p.estimated.nano == nano("0.00006")

"""RateCard resolution and pricing beyond the golden corpus (SPEC §6.2–§6.4, §6.7; Copilot
addendum §6.2)."""

from __future__ import annotations

import dataclasses
import pickle
from decimal import Decimal

import pytest

from tokenbill.core.builders import make_copilot_ctx, make_inference
from tokenbill.core.errors import PricingError, UsageError
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.protocols import Pricer
from tokenbill.core.records import UsageBuckets, UsageSource
from tokenbill.core.testing import FakePricer, assert_pricer_conforms
from tokenbill.core.types import ContractOverlay
from tokenbill.rates.contract import make_overlay
from tokenbill.rates.engine import (
    DQ_COPILOT_BAND_HYPOTHESIS,
    DQ_COPILOT_WRITE_FOLDED,
    DQ_STALE_RATE,
    RateCard,
    no_cache_equivalent_nano,
    price_total,
)
from tokenbill.rates.schema import model_price_layer

from .helpers import builtin, card, ctx, layer, nano, row, ts

DAY = "2026-09-23"
IN_OUT = UsageBuckets(uncached_input=1_000_000, output=1_000_000)


def test_rate_card_conforms() -> None:
    summary = assert_pricer_conforms(card())
    assert len(summary["golden_cases"]) == 32 and summary["unit_rate_samples"] == 200
    assert isinstance(card(), Pricer)


def test_contract_card_conforms() -> None:
    overlay = make_overlay(name="acme", multiplier=Decimal("0.8537"), overrides={},
                           effective_from="2026-01-01")
    contract_card = RateCard([builtin()], contract=overlay)
    assert contract_card.basis is Basis.CONTRACT
    assert assert_pricer_conforms(contract_card, samples=50)["golden_cases"] == []


def test_single_layer_constructor_and_input_validation() -> None:
    assert RateCard(builtin()).rate_card_sha256 == card().rate_card_sha256 == card().sha256
    with pytest.raises(UsageError):
        RateCard([])
    with pytest.raises(UsageError):
        RateCard(["builtin"])  # type: ignore[list-item]
    with pytest.raises(UsageError):
        RateCard([builtin()], contract="acme")  # type: ignore[arg-type]
    bad = ContractOverlay(name="x", multiplier=None, overrides=(("m", (("tokens", Decimal(1)),)),),
                          effective_from="2026-01-01", effective_to=None, derived=False,
                          assumed_fields=())
    with pytest.raises(PricingError):
        RateCard([builtin()], contract=bad)


def test_layer_precedence_contract_over_model_price_over_user_over_builtin() -> None:
    opus = ctx()
    user = layer([row(row_id="u/opus", model="claude-opus-5-5",
                      usd_per_mtok={"input": "3.00", "output": "15.00"},
                      published_absolute={})], name="user:rates.json")
    mp = model_price_layer([("claude-opus-5-5", "2", "10")])
    at = ts(DAY)
    assert card().price_usage(IN_OUT, opus, ts_ms=at).figure.nano == nano("24")
    assert RateCard([builtin(), user]).price_usage(IN_OUT, opus, ts_ms=at).figure.nano == \
        nano("18")
    # --model-price wins over a --rates file whatever the order the layers are given in
    for layers in ([builtin(), user, mp], [mp, user, builtin()]):
        p = RateCard(layers).price_usage(IN_OUT, opus, ts_ms=at)
        assert p.figure.nano == nano("12") and p.lines[0].layer == "user"
    overlay = make_overlay(name="acme", multiplier=None,
                           overrides={"claude-opus-5-5": {"input": Decimal("1"),
                                                          "output": Decimal("1")}},
                           effective_from="2026-01-01")
    p = RateCard([builtin(), user, mp], contract=overlay).price_usage(IN_OUT, opus, ts_ms=at)
    assert p.figure.nano == nano("2") and p.figure.basis is Basis.CONTRACT


def test_later_layers_of_one_kind_win_and_dates_fall_through() -> None:
    a = layer([row(model="claude-opus-5-5", row_id="a", published_absolute={},
                   usd_per_mtok={"input": "3", "output": "3"})], name="user:a")
    b = layer([row(model="claude-opus-5-5", row_id="b", published_absolute={},
                   effective_from="2026-10-01", usd_per_mtok={"input": "1", "output": "1"})],
              name="user:b")
    c = RateCard([builtin(), a, b])
    assert c.price_usage(IN_OUT, ctx(), ts_ms=ts("2026-10-02")).figure.nano == nano("2")
    # before b's first date the lookup falls through to a, then to the registry
    assert c.price_usage(IN_OUT, ctx(), ts_ms=ts(DAY)).figure.nano == nano("6")
    assert RateCard([builtin(), b]).price_usage(IN_OUT, ctx(), ts_ms=ts(DAY)).figure.nano == \
        nano("24")


def test_a_disabled_user_row_shadows_the_registry() -> None:
    off = layer([row(model="claude-opus-5-5", row_id="off", enabled=False)], name="user:off")
    p = RateCard([builtin(), off]).price_usage(IN_OUT, ctx(), ts_ms=ts(DAY))
    assert p.unpriced_reason == "unverified rate row"


def test_effective_date_lookup_across_rows() -> None:
    c = card()
    sonnet = ctx("claude-sonnet-5")
    assert c.resolve(sonnet, ts_ms=ts("2026-06-29")) is None
    assert c.unpriced_reason(sonnet, ts_ms=ts("2026-06-29")) == "model before effective date"
    assert c.resolve(sonnet, ts_ms=ts("2026-06-30", 0)) is not None
    sol = ctx("gpt-5.6-sol", endpoint_scope="global")
    assert c.unpriced_reason(sol, ts_ms=ts("2026-07-08")) == "model before effective date"
    assert c.unpriced_reason(sol, ts_ms=ts("2026-08-20")) == "unverified rate row"
    assert c.resolve(sol, ts_ms=ts("2026-08-21", 0)) is not None
    assert c.unpriced_reason(ctx(""), ts_ms=ts(DAY)) == "not priceable"
    assert c.unpriced_reason(ctx(), ts_ms=ts(DAY)) is None


def test_closed_non_promotional_row_ends_with_no_rate_row() -> None:
    closed = layer([row(effective_to="2026-06-01")])
    c = RateCard([closed])
    assert c.unpriced_reason(ctx("claude-test-1"), ts_ms=ts("2026-07-01")) == "no rate row"


def test_claude_platform_on_aws_and_foundry_fall_back_to_first_party() -> None:
    c = card()
    for channel in ("claude_platform_aws", "foundry"):
        p = c.price_usage(IN_OUT, ctx(channel=channel), ts_ms=ts(DAY))
        assert p.figure.nano == nano("24")
        assert p.lines[0].rate_row_id == "anthropic/anthropic_api/claude-opus-5-5/2026-09-22"
        geo = c.price_usage(IN_OUT, ctx(channel=channel, inference_geo="us"), ts_ms=ts(DAY))
        assert geo.figure.nano == nano("26.4")
    # fast mode is first-party only; Azure has no Claude rows; Copilot never falls back
    assert c.price_usage(IN_OUT, ctx(channel="foundry", speed="fast"),
                         ts_ms=ts(DAY)).figure.nano == nano("24")
    assert c.unpriced_reason(ctx(channel="azure_openai"), ts_ms=ts(DAY)) == "no rate row"
    assert c.unpriced_reason(make_copilot_ctx("claude-mythos-5-1"), ts_ms=ts(DAY)) == \
        "no rate row"


def test_scope_range_only_where_a_regional_premium_applies() -> None:
    c = card()
    # pre-4.5 Bedrock models have no regional premium: an unknown scope stays exact
    old = c.price_usage(IN_OUT, ctx("claude-sonnet-4", channel="bedrock"), ts_ms=ts(DAY))
    assert old.figure.evidence is Evidence.EXACT and old.figure.nano == nano("18")
    new = c.price_usage(IN_OUT, ctx("claude-sonnet-4-5", channel="bedrock"), ts_ms=ts(DAY))
    assert new.figure.evidence is Evidence.ESTIMATED
    assert (new.figure.low_nano, new.figure.high_nano) == (nano("18"), nano("19.8"))
    # multi-region counts as regional (Vertex)
    multi = c.price_usage(IN_OUT, ctx("claude-opus-5-5", channel="vertex",
                                      endpoint_scope="multi_region"), ts_ms=ts(DAY))
    assert multi.figure.nano == nano("26.4") and multi.figure.evidence is Evidence.EXACT
    # scope is ignored on the Claude API
    assert c.price_usage(IN_OUT, ctx(endpoint_scope="regional"), ts_ms=ts(DAY)).figure.nano == \
        nano("24")


def test_unknown_scope_widens_web_search_and_unknown_ttl_writes() -> None:
    usage = UsageBuckets(cache_write_unknown=1_000_000, output=1000, web_search_requests=2)
    p = card().price_usage(usage, ctx("claude-opus-4-6", channel="vertex"), ts_ms=ts(DAY))
    lines = {ln.bucket: (ln.low_nano, ln.high_nano) for ln in p.lines}
    # writes: [5m global 6.25, 1h regional 11.00]; web search is not scaled by the premium
    assert lines["cache_write_unknown"] == (nano("6.25"), nano("11"))
    assert lines["web_search"] == (nano("0.02"), nano("0.02"))
    assert lines["output"] == (nano("0.025"), nano("0.0275"))


def test_write_ttl_hint_moves_the_point() -> None:
    usage = UsageBuckets(cache_write_unknown=1_000_000)
    c = card()
    assert c.price_usage(usage, ctx(write_ttl_hint="1h"), ts_ms=ts(DAY)).figure.nano == \
        nano("8")
    assert c.price_usage(usage, ctx(write_ttl_hint="5m"), ts_ms=ts(DAY)).figure.nano == \
        nano("5")


def test_billable_flags_and_usage_sources() -> None:
    c = card()
    usage = UsageBuckets(uncached_input=1000, output=500)
    full = nano("0.014")
    no = c.price_usage(usage, ctx(), ts_ms=ts(DAY), billable=False)
    assert no.figure.nano == 0 and no.figure.evidence is Evidence.EXACT and len(no.lines) == 2
    maybe = c.price_usage(usage, ctx(), ts_ms=ts(DAY), billable=None)
    assert (maybe.figure.low_nano, maybe.figure.nano, maybe.figure.high_nano) == (0, full, full)
    est = c.price_usage(usage, ctx(), ts_ms=ts(DAY), usage_source=UsageSource.ESTIMATED)
    assert est.figure.evidence is Evidence.ESTIMATED and est.figure.low_nano == full
    assert "reconstructed" in est.figure.note
    part = c.price_usage(usage, ctx(), ts_ms=ts(DAY), usage_source="partial_stream")
    assert (part.figure.low_nano, part.figure.high_nano) == (0, full)
    assert "anthropic.abort.client" in part.figure.note
    ms = c.price_usage(UsageBuckets(uncached_input=10), ctx(), ts_ms=ts(DAY),
                       usage_source=UsageSource.MESSAGE_START_ONLY, output_upper=100)
    out = [ln for ln in ms.lines if ln.bucket == "output"]
    # [0 logged, 100 upper] × $20 → [0, 0.002]
    assert out[0].quantity == 0 and (out[0].low_nano, out[0].high_nano) == (0, 2_000_000)


def test_price_usage_argument_validation() -> None:
    c = card()
    with pytest.raises(UsageError):
        c.price_usage({"output": 1}, ctx(), ts_ms=ts(DAY))  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        c.price_usage(IN_OUT, ctx(), ts_ms=ts(DAY), usage_source="final_ish")
    with pytest.raises(UsageError):
        c.price_usage(IN_OUT, ctx(), ts_ms=ts(DAY), billable=1)  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        c.price_usage(IN_OUT, ctx(), ts_ms=ts(DAY), output_upper=-1)
    with pytest.raises(UsageError):
        c.price_usage(IN_OUT, ctx(), ts_ms=1.5)  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        c.resolve("ctx", ts_ms=ts(DAY))  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        c.price_inference("inf", ts_ms=ts(DAY))  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        c.resolve(ctx(), ts_ms=10**20)


def test_web_search_without_a_rate_is_noted() -> None:
    p = card().price_usage(UsageBuckets(output=10, web_search_requests=1), ctx("claude-opus-5",
                           channel="bedrock", endpoint_scope="global"), ts_ms=ts(DAY))
    assert p.figure.evidence is Evidence.EXACT and "web search requests unpriced" in p.figure.note


def test_fallback_rates_for_buckets_a_row_does_not_price() -> None:
    c = card()
    sol = ctx("gpt-5.6-sol", endpoint_scope="global")
    p = c.price_usage(UsageBuckets(cache_write_5m=100_000), sol, ts_ms=ts(DAY))
    assert p.figure.nano == nano("0.5")      # other-TTL write rate ($5), like FakePricer
    no_1h = ctx("claude-opus-4-1", channel="bedrock", endpoint_scope="global")
    p = c.price_usage(UsageBuckets(cache_write_1h=1_000_000), no_1h, ts_ms=ts(DAY))
    assert p.figure.nano == nano("15")       # no 1h (nor other) write price: the input rate


def test_contract_overlay_scope() -> None:
    overlay = make_overlay(name="acme", multiplier=Decimal("0.5"),
                           overrides={"claude-opus-5-5-20260922": {"output": Decimal("10")},
                                      "claude-haiku-4-5": {"web_search": Decimal("0.001")}},
                           effective_from="2026-09-01", effective_to="2026-10-01",
                           channels=["anthropic_api"])
    c = RateCard([builtin()], contract=overlay)
    usage = UsageBuckets(uncached_input=1_000_000, output=1_000_000)
    # overrides are final prices (keys normalized); the multiplier scales the other buckets
    assert c.price_usage(usage, ctx(), ts_ms=ts(DAY)).figure.nano == nano("12")
    # not on the subscription path, not on other channels, not outside the dates
    sub = c.price_usage(usage, ctx(billing_path="subscription"), ts_ms=ts(DAY))
    assert sub.figure.nano == nano("24") and sub.figure.basis is Basis.LIST_EQUIVALENT
    other = c.price_usage(usage, ctx(channel="foundry"), ts_ms=ts(DAY))
    assert other.figure.nano == nano("24") and other.figure.basis is Basis.LIST
    late = c.price_usage(usage, ctx(), ts_ms=ts("2026-10-01"))
    assert late.figure.nano == nano("24") and late.figure.basis is Basis.LIST
    # per-request overrides are final too; the multiplier scales per-request prices otherwise
    web = UsageBuckets(web_search_requests=10)
    assert c.price_usage(web, ctx("claude-haiku-4-5"), ts_ms=ts(DAY)).figure.nano == \
        nano("0.01")
    assert c.price_usage(web, ctx(), ts_ms=ts(DAY)).figure.nano == nano("0.05")
    # never on Copilot credits
    cp = c.price_usage(usage, make_copilot_ctx("claude-opus-5-5"), ts_ms=ts(DAY))
    assert cp.figure.nano == nano("24") and cp.figure.basis is Basis.LIST_EQUIVALENT
    assert c.list_card().contract is None and card().list_card().contract is None


def test_price_total_keeps_allowance_and_pool_apart() -> None:
    c = card()
    at = ts(DAY)
    billed = make_inference({"uncached_input": 1000, "output": 500})
    ranged = make_inference({"cache_write_unknown": 1000})
    sub = make_inference({"uncached_input": 1000}, ctx=ctx(billing_path="subscription"))
    sub_range = make_inference({"cache_write_unknown": 1000},
                               ctx=ctx(billing_path="subscription"))
    pool = make_inference({"uncached_input": 1000}, ctx=make_copilot_ctx("claude-opus-5-5"))
    direct = make_inference({"uncached_input": 1000},
                            ctx=make_copilot_ctx("claude-opus-5-5", billing_path="copilot_direct"))
    skipped = make_inference({"uncached_input": 10**6}, billable=False)
    total = price_total(c, [(i, at) for i in (billed, ranged, sub, sub_range, pool, direct,
                                              skipped)])
    assert total.exact.nano == nano("0.014") and total.exact.basis is Basis.LIST
    assert total.estimated is not None and total.estimated.nano == nano("0.005")
    assert (total.estimated.low_nano, total.estimated.high_nano) == (nano("0.005"), nano("0.008"))
    assert total.allowance is not None and total.allowance.evidence is Evidence.ESTIMATED
    assert (total.allowance.nano, total.allowance.low_nano, total.allowance.high_nano) == \
        (nano("0.009"), nano("0.009"), nano("0.012"))
    assert total.pool is not None and total.pool.nano == nano("0.008")
    assert total.pool.evidence is Evidence.EXACT and total.pool.basis is Basis.LIST_EQUIVALENT
    assert total.priced_inferences == 6 and total.coverage == "1"
    empty = price_total(c, [])
    assert empty.exact.nano == 0 and empty.allowance is None and empty.coverage == "1"
    with pytest.raises(UsageError):
        price_total(c, [("inf", at)])  # type: ignore[list-item]


def test_price_total_labels_mixed_bases() -> None:
    overlay = make_overlay(name="acme", multiplier=Decimal("0.5"), overrides={},
                           effective_from="2026-01-01", channels=["bedrock"])
    c = RateCard([builtin()], contract=overlay)
    first = make_inference({"uncached_input": 1000})
    br = make_inference({"uncached_input": 1000},
                        ctx=ctx("claude-opus-5", channel="bedrock", endpoint_scope="global"))
    only_contract = price_total(c, [(br, ts(DAY))])
    assert only_contract.exact.basis is Basis.CONTRACT and only_contract.exact.note == ""
    mixed = price_total(c, [(first, ts(DAY)), (br, ts(DAY))])
    assert mixed.exact.basis is Basis.CONTRACT and "mixed bases" in mixed.exact.note
    assert price_total(card(), [(br, ts(DAY))]).exact.basis is Basis.LIST


def test_price_total_matches_the_fake_on_facts_rows() -> None:
    from tokenbill.core.testing import fake_price_total
    items = [(make_inference({"uncached_input": 1000 * i, "cache_read": 500 * i, "output": 7 * i},
                             model=m), ts(DAY))
             for i, m in enumerate(["claude-opus-5-5", "claude-sonnet-5", "claude-fable-5-1",
                                    "claude-haiku-4-5", "claude-opus-4-8"], start=1)]
    assert price_total(card(), items) == fake_price_total(FakePricer(), items)


def test_no_cache_equivalent_nano() -> None:
    inf = make_inference({"uncached_input": 1000, "cache_read": 100_000, "cache_write_5m": 2000,
                          "cache_write_1h": 3000, "output": 500},
                         ctx=ctx(service_tier="batch"))
    # 106,000 input × $4 + 500 × $20 at list, no batch discount = 0.424 + 0.010
    assert no_cache_equivalent_nano(card(), inf, ts(DAY)) == nano("0.434")
    overlay = make_overlay(name="acme", multiplier=Decimal("0.5"), overrides={},
                           effective_from="2026-01-01")
    assert no_cache_equivalent_nano(RateCard([builtin()], contract=overlay), inf, ts(DAY)) == \
        nano("0.434")
    assert no_cache_equivalent_nano(FakePricer(), inf, ts(DAY)) == nano("0.434")
    assert no_cache_equivalent_nano(card(), dataclasses.replace(inf, billable=False),
                                    ts(DAY)) == 0
    unknown = make_inference({"uncached_input": 5}, model="claude-foo-9")
    assert no_cache_equivalent_nano(card(), unknown, ts(DAY)) == 0
    with pytest.raises(UsageError):
        no_cache_equivalent_nano(card(), "inf", ts(DAY))  # type: ignore[arg-type]


def test_stale_rows_and_data_quality() -> None:
    old = layer([row(verified_on="2026-07-01")])
    c = RateCard([old, model_price_layer([("claude-z", "1", "1")])])
    assert c.data_quality(today="2026-08-30") == []
    c.price_usage(IN_OUT, ctx("claude-test-1"), ts_ms=ts("2026-08-30"))
    c.price_usage(IN_OUT, ctx("claude-z"), ts_ms=ts("2026-08-30"))
    assert c.used_rows() == ("model-price/*/claude-z/1970-01-01",
                             "test/anthropic_api/claude-test-1/2026-01-01")
    # 60 days after verification: stale; 45 days: not yet; --model-price rows are exempt
    assert c.stale_rows(today="2026-08-30") == ("test/anthropic_api/claude-test-1/2026-01-01",)
    assert c.stale_rows(today="2026-08-15") == ()
    notes = c.data_quality(today="2026-08-30")
    assert [(n.code, n.count, n.severity) for n in notes] == [(DQ_STALE_RATE, 1, "warn")]
    info = c.info(today="2026-08-30")
    assert info.stale_rows == ("test/anthropic_api/claude-test-1/2026-01-01",)
    assert info.layers == ("user:test", "model-price") and info.contract is None
    assert info.basis is Basis.LIST and info.sha256 == c.rate_card_sha256
    assert c.stale_rows(today="2026-08-30", row_ids=["nope"]) == ()


def test_rows_and_modifiers_introspection() -> None:
    c = card()
    assert len(c.rows()) == len(builtin().rows)
    assert [m.modifier_id for m in c.modifiers()] == sorted(m.modifier_id for m in c.modifiers())


def test_card_pickles_without_its_caches() -> None:
    c = card()
    c.price_usage(IN_OUT, ctx(), ts_ms=ts(DAY))
    clone = pickle.loads(pickle.dumps(c))
    assert clone.rate_card_sha256 == c.rate_card_sha256
    assert clone.price_usage(IN_OUT, ctx(), ts_ms=ts(DAY)) == c.price_usage(IN_OUT, ctx(),
                                                                            ts_ms=ts(DAY))


def test_caches_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import tokenbill.rates.engine as engine
    monkeypatch.setattr(engine, "_CACHE_LIMIT", 2)
    c = card()
    for day in range(1, 6):
        c.price_usage(IN_OUT, ctx(), ts_ms=ts(f"2026-10-0{day}"))
    assert len(c._find_cache) <= 2 and len(c._rates_cache) <= 2


def test_unit_rates_none_when_unpriced_or_web_search_not_whole_nano() -> None:
    c = card()
    assert c.unit_rates(ctx("claude-foo-9"), ts_ms=ts(DAY)) is None
    odd = layer([row(per_request_usd={"web_search": "0.0000000001"})])
    assert RateCard([odd]).unit_rates(ctx("claude-test-1"), ts_ms=ts(DAY)) is None


# ---------- GitHub Copilot (addendum §6.2) ----------


def test_copilot_paths_are_list_equivalent_into_the_pool() -> None:
    usage = UsageBuckets(uncached_input=12_000, cache_read=180_000, cache_write_unknown=6000,
                         output=3000)
    c = card()
    for path in ("copilot_pool", "copilot_direct"):
        p = c.price_usage(usage, make_copilot_ctx("claude-opus-5-5", billing_path=path),
                          ts_ms=ts(DAY))
        assert p.figure.basis is Basis.LIST_EQUIVALENT
        assert (p.figure.nano, p.figure.low_nano, p.figure.high_nano) == \
            (174_000_000, 174_000_000, 192_000_000)                       # C.G1, C.G13
        assert p.exact_nano == 144_000_000


def test_copilot_utility_call_without_a_row_is_exact_zero() -> None:
    # C.G12: gpt-4o-mini utility call (nano-AIU 0), billable False → EXACT $0, not a gap
    p = card().price_usage(UsageBuckets(uncached_input=500, output=20),
                           make_copilot_ctx("gpt-4o-mini"), ts_ms=ts(DAY), billable=False)
    assert p.figure.nano == 0 and p.figure.evidence is Evidence.EXACT and p.unpriced_reason is None
    unpriced = card().price_usage(UsageBuckets(uncached_input=500), make_copilot_ctx("gpt-4o-mini"),
                                  ts_ms=ts(DAY))
    assert unpriced.figure.nano is None and unpriced.unpriced_reason == "no rate row"


def test_copilot_writes_folded_to_input() -> None:
    # C.G15: gpt-5.3-codex has no write price: 1,000 writes at the $1.75 input rate, ESTIMATED
    c = card()
    for usage in (UsageBuckets(cache_write_unknown=1000), UsageBuckets(cache_write_5m=1000)):
        p = c.price_usage(usage, make_copilot_ctx("gpt-5.3-codex"), ts_ms=ts(DAY))
        assert p.figure.nano == 1_750_000 and p.figure.evidence is Evidence.ESTIMATED
        assert (p.figure.low_nano, p.figure.high_nano) == (1_750_000, 1_750_000)
        assert DQ_COPILOT_WRITE_FOLDED in p.figure.note


def test_copilot_band_hypotheses() -> None:
    small = UsageBuckets(uncached_input=20_000, cache_read=250_000, output=4000)
    big = UsageBuckets(uncached_input=20_000, cache_read=280_000, output=4000)
    c = card()
    at = ts("2026-09-10")
    assert c.price_usage(small, make_copilot_ctx("gpt-5.5"), ts_ms=at).figure.nano == 345_000_000
    both = c.price_usage(small, make_copilot_ctx("gpt-5.5", context_tier="long_context"),
                         ts_ms=at)
    assert (both.figure.nano, both.figure.low_nano, both.figure.high_nano) == \
        (345_000_000, 345_000_000, 630_000_000)                            # C.G5b
    assert DQ_COPILOT_BAND_HYPOTHESIS in both.figure.note
    # B agrees with A: exact either way
    agree = c.price_usage(big, make_copilot_ctx("gpt-5.5", context_tier="long_context"),
                          ts_ms=at)
    assert agree.figure.evidence is Evidence.EXACT and agree.figure.nano == 660_000_000
    # A says band (300,000 > 272,000), B says default: range [default, band], point A:
    # band 20,000 × 10 + 280,000 × 1 + 4,000 × 45 = 0.66; base 0.1 + 0.14 + 0.12 = 0.36
    down = c.price_usage(big, make_copilot_ctx("gpt-5.5", context_tier="default"), ts_ms=at)
    assert (down.figure.nano, down.figure.low_nano, down.figure.high_nano) == \
        (660_000_000, 360_000_000, 660_000_000)
    # rows without a band are never widened
    plain = c.price_usage(small, make_copilot_ctx("claude-opus-5-5", context_tier="long_context"),
                          ts_ms=ts(DAY))
    assert plain.figure.evidence is Evidence.EXACT


def test_copilot_modifiers_auto_and_compliance() -> None:
    c = card()
    usage = UsageBuckets(uncached_input=1_000_000)
    at = ts(DAY)
    assert c.price_usage(usage, make_copilot_ctx("claude-sonnet-5", routing="auto"),
                         ts_ms=at).figure.nano == nano("1.8")
    both = c.resolve(make_copilot_ctx("claude-sonnet-5", routing="auto", compliance="fedramp"),
                     ts_ms=at)
    assert both is not None and both.input == Decimal("1.98") and both.stacking_assumed
    assert set(both.modifier_ids) == {"github.auto", "github.compliance"}

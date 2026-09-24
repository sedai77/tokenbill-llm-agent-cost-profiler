"""Integer unit rates on the hot path agree with ``price_usage`` to the nano (SPEC §6.4, §9.1 #3):
differential against a Decimal-only pricer, probe rejection of wrong unit rates, the long-context
band, contract multipliers that need a scale above 9, and FlatRates."""

from __future__ import annotations

import json
import random
from decimal import Decimal
from typing import Any

import pytest

from tokenbill.core.builders import FlatRates, make_ctx
from tokenbill.core.labels import Basis
from tokenbill.core.records import UsageBuckets, to_json
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import ContractOverlay, Policy, UnitRates
from tokenbill.sim.usage_replay import _buckets, _PriceBook, _tup

from .helpers import SDK, at, outcomes, priced_request, random_lane, replay, table


class DecimalOnly(FakePricer):
    """FakePricer without unit rates: every line goes through ``price_usage``."""

    def unit_rates(self, ctx: Any, *, ts_ms: int) -> UnitRates | None:
        return None


class WrongUnits(FakePricer):
    """FakePricer whose unit rates belong to another model (the probe must reject them)."""

    def unit_rates(self, ctx: Any, *, ts_ms: int) -> UnitRates | None:
        other = make_ctx("claude-haiku-4-5", channel=ctx.channel)
        return super().unit_rates(other, ts_ms=ts_ms)


class Exploding(FakePricer):
    """price_usage raises for probe-sized inputs; the book must fall back, never crash."""

    def price_usage(self, usage: UsageBuckets, ctx: Any, **kw: Any) -> Any:
        if usage.cache_write_other == 1_000 and usage.web_search_requests == 1:
            raise ArithmeticError("probe")
        return super().price_usage(usage, ctx, **kw)


_POLICIES = ("observed", "ttl=1h", "ttl=5m", "keepalive=240s,max=3600s", "fast=off;geo=global",
             "model=claude-sonnet-4-6@lane_kind:main", "effort=low,scale=0.25",
             "compact-window=20000,post=9000;cold-resume=clear,min=10000", "batch=eligible",
             "repair=restore_caching;repair=fallback_credit;repair=retry_backoff_cap")


def _dump(res: object) -> str:
    return json.dumps(to_json(res), sort_keys=True)


@pytest.mark.parametrize("spec", _POLICIES)
def test_hot_path_equals_the_decimal_path(spec: str) -> None:
    lanes = [random_lane(random.Random(900 + i), f"d{i}") for i in range(60)]
    fast = replay(lanes, spec)
    slow = replay(lanes, spec, pricer=DecimalOnly())
    assert _dump(fast) == _dump(slow)


def test_wrong_unit_rates_are_rejected_by_the_probe() -> None:
    lanes = [random_lane(random.Random(40 + i), f"w{i}") for i in range(30)]
    for spec in ("observed", "ttl=1h", "keepalive=240s,max=3600s"):
        assert _dump(replay(lanes, spec, pricer=WrongUnits())) == _dump(replay(lanes, spec))
    book = _PriceBook(WrongUnits())
    assert book.unit(make_ctx("claude-opus-5-5"), at(0)) is None


def test_a_failing_probe_falls_back() -> None:
    book = _PriceBook(Exploding())
    assert book.unit(make_ctx("claude-opus-5-5"), at(0)) is None
    t = _tup(UsageBuckets(cache_read=1_000, output=10))
    assert book.price(t, make_ctx("claude-opus-5-5"), at(0)) == (400_000, 400_000, 400_000,
                                                                 False)


def test_long_context_band_is_found_and_priced_through_price_usage() -> None:
    book = _PriceBook(FakePricer())
    ctx = make_ctx("gpt-5.6-sol")
    unit = book.unit(ctx, at(0))
    assert unit is not None and unit.limit == 272_001   # the band applies above 272,000
    assert book.unit(make_ctx("claude-opus-5-5"), at(0)).limit is None
    below = _tup(UsageBuckets(uncached_input=272_000, output=1_000))
    above = _tup(UsageBuckets(uncached_input=300_000, output=1_000))
    for t in (below, above):
        fig = FakePricer().price_usage(_buckets(t), ctx, ts_ms=at(0)).figure
        assert book.price(t, ctx, at(0))[0] == fig.nano
    # SPEC §6.9 case 11 (band): 300,000 × $8/M + 1,000 × $30/M
    assert book.price(above, ctx, at(0))[0] == 2_430_000_000


def test_openai_lanes_replay_to_the_nano_across_the_band() -> None:
    from tokenbill.core.records import RequestParams

    params = RequestParams(model_requested="gpt-5.6-sol", effort="high")
    lane = table([(0, 0, 0, 0, 250_000, 2_000), (60, 0, 0, 0, 290_000, 2_000)],
                 model="gpt-5.6-sol", attribution=SDK, params=params)
    fast = replay(lane, "effort=low,scale=0.5")
    slow = replay(lane, "effort=low,scale=0.5", pricer=DecimalOnly())
    assert _dump(fast) == _dump(slow)
    assert fast.baseline.nano == sum(priced_request(r).nano for r in lane.requests)


def test_ttl_policy_is_skipped_on_openai_channels() -> None:
    lane = table([(0, 0, 0, 0, 20_000, 100), (60, 0, 0, 0, 21_000, 100)], model="gpt-5.6-sol",
                 attribution=SDK)
    res = replay(lane, "ttl=1h")
    assert res.lanes_skipped == ((lane.lane_key, "ttl policy not applicable on channel "
                                                 "openai_api"),)
    assert res.saving.nano == 0


def _contract(multiplier: str) -> ContractOverlay:
    return ContractOverlay(name="acme", multiplier=Decimal(multiplier), overrides=(),
                           effective_from="2026-01-01", effective_to=None, derived=False,
                           assumed_fields=())


def test_contract_multiplier_needs_a_large_scale() -> None:
    pricer = FakePricer().with_contract(_contract("0.8537"))
    ur = pricer.unit_rates(make_ctx("claude-opus-5-5"), ts_ms=at(0))
    assert ur is not None and ur.scale_exp > 9
    lanes = [random_lane(random.Random(70 + i), f"c{i}", allow_unpriced=False)
             for i in range(25)]
    for spec in ("observed", "ttl=1h", "keepalive=240s,max=3600s"):
        fast = replay(lanes, spec, pricer=pricer)
        slow = replay(lanes, spec, pricer=DecimalOnly(contract=_contract("0.8537")))
        assert slow.cost.nano is not None
        assert (fast.cost.nano, fast.cost.low_nano, fast.cost.high_nano) == \
            (slow.cost.nano, slow.cost.low_nano, slow.cost.high_nano)
        assert fast.baseline.basis is Basis.CONTRACT


def test_flat_rates() -> None:
    lane = table([(0, 0, 100_000, 0, 0, 500), (420, 0, 102_000, 0, 0, 500)], attribution=SDK)
    res = replay(lane, "ttl=1h", pricer=FlatRates())
    o = outcomes(res)
    assert res.baseline.nano == sum(priced_request(r, FlatRates()).nano for r in lane.requests)
    # FlatRates: 1h writes $2/M, reads $0.10/M, output $5/M
    second = o[lane.requests[1].request_id]
    assert second.cost_nano == 10_000_000 + 4_000_000 + 2_500_000


def test_unit_rate_keys_are_per_day() -> None:
    book = _PriceBook(FakePricer())
    ctx = make_ctx("claude-opus-5-5")
    assert book.unit(ctx, at(0)) is book.unit(ctx, at(3_600))
    before = at(-2 * 86_400)                            # 2026-09-21: Opus 5.5 not yet priced
    assert book.unit(ctx, before) is None
    assert book.price(_tup(UsageBuckets(output=10)), ctx, before) is None
    assert "model before effective date" in book.unpriced_reasons


def test_non_billable_inferences_cost_nothing() -> None:
    book = _PriceBook(FakePricer())
    t = _tup(UsageBuckets(uncached_input=100))
    assert book.price(t, make_ctx("claude-not-a-model"), at(0), billable=False) == (0, 0, 0,
                                                                                    False)


def test_web_fetch_goes_through_price_usage() -> None:
    book = _PriceBook(FakePricer())
    t = _tup(UsageBuckets(uncached_input=100, web_fetch_requests=2))
    fig = FakePricer().price_usage(_buckets(t), make_ctx("claude-opus-5-5"), ts_ms=at(0)).figure
    assert book.price(t, make_ctx("claude-opus-5-5"), at(0))[0] == fig.nano


def test_observed_policy_uses_the_same_numbers_on_every_pricer() -> None:
    lanes = [random_lane(random.Random(i), f"p{i}") for i in range(20)]
    for pricer in (FakePricer(), DecimalOnly(), FlatRates()):
        res = replay(lanes, Policy.observed(), pricer=pricer)
        assert res.cost == res.baseline

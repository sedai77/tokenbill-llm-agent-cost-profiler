"""Gate tests (merge gate 1): the engine and the gate with RATES' real ``RateCard``.

Skipped until ``tokenbill.rates`` is merged (``importorskip``). The RateCard's registry equals
``core/facts.json`` on every shared row (D37 parity), so Appendix A holds to the nano with it, and
its integer unit rates must agree with its own ``price_usage`` (SPEC §6.4 differential)."""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from tokenbill.core.records import to_json
from tokenbill.core.testing import assert_replayer_conforms
from tokenbill.core.types import UnitRates
from tokenbill.sim.calibrate import calibrate_lanes
from tokenbill.sim.usage_replay import UsageReplayer

from .helpers import RULES, a1_lane, a2_lane, a2b_lane, a3_lane, a6_lane, random_lane, replay, usd

pytestmark = pytest.mark.gate

engine_mod = pytest.importorskip("tokenbill.rates.engine")
schema_mod = pytest.importorskip("tokenbill.rates.schema")


def _rate_card() -> Any:
    """The built-in RateCard (SPEC §6 fixes ``load_builtin()`` but not the constructor)."""
    layer = schema_mod.load_builtin()
    for build in (lambda: engine_mod.RateCard([layer]), lambda: engine_mod.RateCard((layer,)),
                  lambda: engine_mod.RateCard(layers=[layer]),
                  lambda: engine_mod.RateCard(layer)):
        try:
            return build()
        except TypeError:
            continue
    pytest.fail("cannot build a RateCard from load_builtin()")


class _NoUnits:
    """Delegates to a Pricer but has no unit rates: every line through ``price_usage``."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.basis = inner.basis
        self.rate_card_sha256 = inner.rate_card_sha256

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def resolve(self, ctx: Any, *, ts_ms: int) -> Any:
        return self.inner.resolve(ctx, ts_ms=ts_ms)

    def price_inference(self, inf: Any, *, ts_ms: int) -> Any:
        return self.inner.price_inference(inf, ts_ms=ts_ms)

    def price_usage(self, usage: Any, ctx: Any, **kw: Any) -> Any:
        return self.inner.price_usage(usage, ctx, **kw)

    def unit_rates(self, ctx: Any, *, ts_ms: int) -> UnitRates | None:
        return None

    def min_cacheable_tokens(self, ctx: Any, *, ts_ms: int) -> int | None:
        return self.inner.min_cacheable_tokens(ctx, ts_ms=ts_ms)

    def supports(self, ctx: Any, feature: str, *, ts_ms: int) -> bool:
        return self.inner.supports(ctx, feature, ts_ms=ts_ms)

    def tokenizer_family(self, ctx: Any, *, ts_ms: int) -> str | None:
        return self.inner.tokenizer_family(ctx, ts_ms=ts_ms)


@pytest.fixture(scope="module")
def card() -> Any:
    return _rate_card()


def test_conforms_with_the_rate_card(card: Any) -> None:
    assert_replayer_conforms(UsageReplayer(), card, rules=RULES)


def test_appendix_a_with_the_rate_card(card: Any) -> None:
    assert replay(a1_lane(), "ttl=1h", pricer=card).saving.nano == usd("1.1508")
    assert replay(a2_lane(), "ttl=1h", pricer=card).saving.nano == -usd("0.318")
    assert replay(a2b_lane(), "ttl=5m", pricer=card).saving.nano == usd("0.318")
    assert replay(a3_lane(), "ttl=5m", pricer=card).saving.nano == -usd("1.1508")
    assert replay(a1_lane(), "keepalive=240s,max=3600s", pricer=card).cost.nano == usd("0.6924")
    assert replay(a6_lane(), "compact-window=400000,post=20000", pricer=card).cost.nano == \
        usd("1.999")


@pytest.mark.parametrize("spec", ["observed", "ttl=1h", "keepalive=240s,max=3600s",
                                  "model=claude-sonnet-4-6;fast=off", "batch=eligible"])
def test_rate_card_unit_rates_agree_with_its_decimal_path(card: Any, spec: str) -> None:
    lanes = [random_lane(random.Random(300 + i), f"g{i}") for i in range(40)]
    fast = replay(lanes, spec, pricer=card)
    slow = replay(lanes, spec, pricer=_NoUnits(card))
    assert json.dumps(to_json(fast), sort_keys=True) == json.dumps(to_json(slow), sort_keys=True)


def test_calibrate_with_the_rate_card(card: Any) -> None:
    report = calibrate_lanes([a1_lane(), a2_lane()], pricer=card, rules=RULES)
    assert report.status in ("pass", "fail", "insufficient_data")

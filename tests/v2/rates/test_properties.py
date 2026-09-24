"""Property tests (hypothesis): unit rates agree with ``price_usage`` to the nano (SPEC §6.4), the
per-line exactness identities (§6.3), ``price_total`` partitions (D26, C-15) and the Copilot band
hypotheses (addendum §6.2 #4)."""

from __future__ import annotations

import json
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.core.builders import make_copilot_ctx, make_inference
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.money import EXACT_CTX, token_nano
from tokenbill.core.records import UsageBuckets
from tokenbill.rates.contract import make_overlay
from tokenbill.rates.engine import RateCard, price_total
from tokenbill.rates.schema import parse_layer

from .helpers import card, ctx, doc, row, ts

SETTINGS = settings(max_examples=150, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])


def _decimal(max_int: int, max_places: int) -> st.SearchStrategy[Decimal]:
    return st.builds(lambda i, places, frac: Decimal(i) + Decimal(frac).scaleb(-places),
                     st.integers(0, max_int), st.integers(0, max_places),
                     st.integers(0, 10**6)).filter(lambda d: d.as_tuple().exponent >= -18)


def _dstr(d: Decimal) -> str:
    text = format(d, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


usages = st.builds(
    UsageBuckets,
    uncached_input=st.integers(0, 60_000), cache_read=st.integers(0, 120_000),
    cache_write_5m=st.integers(0, 30_000), cache_write_1h=st.integers(0, 30_000),
    cache_write_unknown=st.integers(0, 10_000), output=st.integers(0, 20_000),
    web_search_requests=st.integers(0, 4))


def _min_exponent(d: Decimal) -> int:
    return d.normalize(EXACT_CTX).as_tuple().exponent if d else 0  # type: ignore[return-value]


@SETTINGS
@given(inp=_decimal(40, 12), out=_decimal(200, 8),
       read=st.sampled_from(["0.025", "0.05", "0.1", "0.125", "0.0625"]),
       batch=st.sampled_from([None, "0.5", "0.55", "0.333"]),
       geo=st.sampled_from([None, "1.1", "1.05"]),
       contract=st.one_of(st.none(), _decimal(1, 6).filter(lambda d: Decimal("0.01") <= d <= 3)),
       usage=usages)
def test_unit_rates_agree_with_price_usage(inp: Decimal, out: Decimal, read: str,
                                           batch: str | None, geo: str | None,
                                           contract: Decimal | None, usage: UsageBuckets) -> None:
    mods = []
    if batch:
        mods.append({"modifier_id": "t.batch", "kind": "multiply", "factor": batch,
                     "applies_to": ["*"], "when": {"service_tier": "batch"},
                     "stacking": "documented"})
    if geo:
        mods.append({"modifier_id": "t.geo", "kind": "multiply", "factor": geo,
                     "applies_to": ["input", "output", "cache_read"],
                     "when": {"inference_geo": "us"}, "stacking": "assumed"})
    r = row(usd_per_mtok={"input": _dstr(inp), "output": _dstr(out)},
            multipliers={"cache_read": read, "cache_write_5m": "1.25", "cache_write_1h": "2"},
            published_absolute={})
    lay = parse_layer(json.dumps(doc([r], mods)), "user:prop")
    overlay = None if contract is None else make_overlay(
        name="p", multiplier=contract, overrides={}, effective_from="2026-01-01")
    c = RateCard([lay], contract=overlay)
    pctx = ctx("claude-test-1", service_tier="batch" if batch else "standard",
               inference_geo="us" if geo else None)
    at = ts("2026-09-23")
    priced = c.price_usage(usage, pctx, ts_ms=at)
    rates = c.resolve(pctx, ts_ms=at)
    assert rates is not None and priced.figure.nano is not None
    unit = c.unit_rates(pctx, ts_ms=at)
    buckets = [rates.input, rates.output, rates.cache_read, rates.cache_write_5m,
               rates.cache_write_1h]
    needed = max(6, 6 - min(_min_exponent(b) for b in buckets if b is not None))
    web = dict(rates.per_request).get("web_search", Decimal(0))
    if unit is None:
        assert needed > 24 or _min_exponent(web) < -9
        return
    assert needed <= 24 and unit.scale_exp == needed
    total = 0
    for line in priced.lines:
        got = unit.bucket_nano(line.bucket, line.quantity)
        assert got == line.amount_nano, line.bucket
        total += got
    assert total == priced.figure.nano


@SETTINGS
@given(usage=usages,
       model=st.sampled_from(["claude-opus-5-5", "claude-fable-5-1", "claude-haiku-4-5",
                              "claude-sonnet-4-5", "claude-opus-4-1", "claude-3-5-haiku"]),
       channel=st.sampled_from(["anthropic_api", "bedrock", "vertex", "foundry"]),
       scope=st.sampled_from(["global", "regional", "unknown"]),
       tier=st.sampled_from(["standard", "batch", "priority"]),
       billable=st.sampled_from([True, None, False]),
       hint=st.sampled_from([None, "5m", "1h"]))
def test_per_line_identities(usage: UsageBuckets, model: str, channel: str, scope: str,
                             tier: str, billable: bool | None, hint: str | None) -> None:
    c = card()
    p = c.price_usage(usage, ctx(model, channel=channel, endpoint_scope=scope, service_tier=tier,
                                 write_ttl_hint=hint), ts_ms=ts("2026-09-23"), billable=billable)
    if p.figure.nano is None:
        assert p.lines == () and p.unpriced_reason
        return
    exact_lines = [ln for ln in p.lines if ln.exact]
    ranged = [ln for ln in p.lines if not ln.exact]
    assert p.exact_nano == sum(ln.amount_nano for ln in exact_lines)
    assert p.figure.nano == sum(ln.amount_nano for ln in p.lines)
    for ln in p.lines:
        if ln.bucket == "web_search" or billable is False:
            continue
        if ln.exact or ln.bucket != "cache_write_unknown":
            assert ln.amount_nano == token_nano(ln.quantity, Decimal(ln.unit_usd_per_mtok))
        if not ln.exact:
            assert ln.low_nano is not None and ln.high_nano is not None
            assert ln.low_nano <= ln.amount_nano <= ln.high_nano
    if ranged:
        assert p.estimated is not None and p.figure.evidence is Evidence.ESTIMATED
        assert p.figure.low_nano == p.exact_nano + sum(ln.low_nano or 0 for ln in ranged)
        assert p.figure.high_nano == p.exact_nano + sum(ln.high_nano or 0 for ln in ranged)
    else:
        assert p.estimated is None and p.figure.evidence is Evidence.EXACT


paths = st.sampled_from(["api_key", "subscription", "copilot_pool", "copilot_direct",
                         "usage_credits"])


@SETTINGS
@given(items=st.lists(st.tuples(paths, st.integers(0, 50_000), st.integers(0, 5_000),
                                st.integers(0, 3_000), st.sampled_from([True, None, False])),
                      max_size=12))
def test_price_total_never_mixes_allowance_or_pool_into_exact(
        items: list[tuple[str, int, int, int, bool | None]]) -> None:
    c = card()
    at = ts("2026-09-23")
    infs = []
    for path, uncached, output, unknown, billable in items:
        usage = {"uncached_input": uncached, "output": output, "cache_write_unknown": unknown}
        pctx = make_copilot_ctx("claude-opus-5-5", billing_path=path) \
            if path.startswith("copilot") else ctx(billing_path=path)
        infs.append(make_inference(usage, ctx=pctx, billable=billable))
    total = price_total(c, [(i, at) for i in infs])

    def point(kind: str) -> int:
        out = 0
        for inf in infs:
            if inf.billable is False:
                continue
            p = c.price_inference(inf, ts_ms=at)
            path = inf.pricing.billing_path
            group = "pool" if path.startswith("copilot") else \
                "allowance" if path == "subscription" else "billed"
            if group == kind:
                out += p.exact_nano if kind == "billed" else p.figure.nano  # type: ignore[operator]
        return out

    assert total.exact.nano == point("billed") and total.exact.basis is Basis.LIST
    assert (total.allowance.nano if total.allowance else 0) == point("allowance")
    assert (total.pool.nano if total.pool else 0) == point("pool")
    for fig in (total.allowance, total.pool):
        assert fig is None or (fig.basis is Basis.LIST_EQUIVALENT and not fig.is_billed_eligible)
    assert total.priced_inferences == sum(1 for i in infs if i.billable is not False)


@SETTINGS
@given(uncached=st.integers(0, 150_000), read=st.integers(0, 300_000),
       output=st.integers(0, 20_000),
       tier=st.sampled_from([None, "default", "long_context"]),
       model=st.sampled_from(["gpt-5.5", "gpt-5.6-sol", "grok-4.7", "gpt-5.6-luna"]))
def test_copilot_band_hypotheses(uncached: int, read: int, output: int, tier: str | None,
                                 model: str) -> None:
    c = card()
    at = ts("2026-09-23")
    usage = UsageBuckets(uncached_input=uncached, cache_read=read, output=output)
    p = c.price_usage(usage, make_copilot_ctx(model, context_tier=tier), ts_ms=at)
    a = c.price_usage(usage, make_copilot_ctx(model), ts_ms=at)          # hypothesis A alone
    assert a.figure.evidence is Evidence.EXACT and p.figure.nano == a.figure.nano
    row = next(r for r in c.rows() if r.channel == "github_copilot" and r.model == model
               and r.effective_from <= "2026-09-23" and r.effective_to is None)
    above = usage.total_input > row.long_context_threshold  # type: ignore[operator]

    def row_price(band: bool) -> int:
        rates = {"input": row.input_usd_per_mtok, "output": row.output_usd_per_mtok,
                 "cache_read": EXACT_CTX.multiply(row.input_usd_per_mtok,
                                                  row.cache_read_mult)}  # type: ignore[arg-type]
        if band:
            rates.update(dict(row.long_context_usd_per_mtok))
        return (token_nano(uncached, rates["input"]) + token_nano(read, rates["cache_read"])
                + token_nano(output, rates["output"]))

    assert a.figure.nano == row_price(above)
    if tier is None or above == (tier == "long_context") or not (usage.total_input or output):
        assert p.figure.evidence is Evidence.EXACT
        return
    low, high = sorted([row_price(above), row_price(not above)])
    assert p.figure.evidence is Evidence.ESTIMATED
    assert "dq.copilot_band_hypothesis" in p.figure.note
    assert (p.figure.low_nano, p.figure.nano, p.figure.high_nano) == (low, row_price(above), high)


@SETTINGS
@given(tokens=st.integers(0, 2**53), digits=st.integers(0, 10**30),
       exponent=st.integers(-40, 3))
def test_integer_line_path_equals_the_decimal_path(tokens: int, digits: int,
                                                   exponent: int) -> None:
    from tokenbill.rates.engine import _line_nano

    rate = Decimal(digits).scaleb(exponent)
    assert _line_nano(tokens, rate) == token_nano(tokens, rate)

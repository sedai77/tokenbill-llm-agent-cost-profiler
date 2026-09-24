"""Rate transforms (SPEC §9.3.5): model remap with the tokenizer band, selector-scoped effort,
``fast=off`` (incl. the fast-toggle flips), ``geo=global``, ``regional=global`` and ``batch``."""

from __future__ import annotations

import pytest

from tokenbill.core.labels import Evidence
from tokenbill.core.records import LaneKind, RequestParams, UsageBuckets
from tokenbill.core.types import Policy

from .helpers import (
    CC,
    SDK,
    outcomes,
    price,
    priced_request,
    replay,
    same,
    table,
    usd,
)

# ------------------------------------------------------------------------------ model remap


def _sonnet46_lane(**kw: object) -> object:
    return table([(0, 0, 10_000, 0, 0, 1_000), (30, 10_000, 1_000, 0, 0, 1_000)],
                 model="claude-sonnet-4-6", attribution=CC, **kw)


def test_remap_across_families_applies_the_tokenizer_band() -> None:
    """legacy → 4.7+: point 1.00, bounds [1.00, 1.35] (outward rounding)."""
    lane = _sonnet46_lane()
    res = replay(lane, "model=claude-sonnet-5")
    assert res.baseline.nano == usd("0.07425")
    # point: Sonnet 5 rates on the same tokens
    assert res.cost.nano == usd("0.025") + usd("0.01") + usd("0.002") + usd("0.0025") + \
        usd("0.01")
    # high: every quantity × 1.35
    assert res.cost.high_nano == usd("0.03375") + usd("0.0135") + usd("0.0027") + \
        usd("0.003375") + usd("0.0135")
    assert res.cost.low_nano == res.cost.nano
    assert res.saving.nano == usd("0.07425") - res.cost.nano
    assert res.saving.low_nano == usd("0.07425") - res.cost.high_nano
    assert res.saving.high_nano == res.saving.nano
    assert res.saving.upper_bound
    assert any("needs_eval" in a for a in res.assumptions)
    o = outcomes(res)
    assert all(x.low_nano is not None for x in o.values())


def test_remap_from_4_7_to_legacy_scales_down() -> None:
    lane = table([(0, 0, 27_000, 0, 0, 2_700)], model="claude-sonnet-5", attribution=CC)
    res = replay(lane, "model=claude-sonnet-4-6")
    point = price(UsageBuckets(cache_write_5m=27_000, output=2_700), "claude-sonnet-4-6")
    low = price(UsageBuckets(cache_write_5m=20_000, output=2_000), "claude-sonnet-4-6")
    assert (res.cost.nano, res.cost.low_nano, res.cost.high_nano) == (point, low, point)


def test_same_tier_upgrade_has_no_band() -> None:
    lane = table([(0, 0, 10_000, 0, 0, 1_000), (30, 10_000, 1_000, 0, 0, 1_000)],
                 model="claude-opus-5", attribution=CC)
    res = replay(lane, "model=claude-opus-5-5@model:claude-opus-5")
    assert res.cost.low_nano is None and res.saving.low_nano is None
    expected = price(UsageBuckets(cache_write_5m=10_000, output=1_000)) + \
        price(UsageBuckets(cache_read=10_000, cache_write_5m=1_000, output=1_000))
    assert res.cost.nano == expected
    assert res.saving.evidence is Evidence.ESTIMATED


def test_remap_within_a_family_has_no_band() -> None:
    lane = table([(0, 0, 10_000, 0, 0, 1_000)], attribution=CC)
    res = replay(lane, "model=claude-sonnet-5")
    assert res.cost.low_nano is None
    assert res.cost.nano == price(UsageBuckets(cache_write_5m=10_000, output=1_000),
                                  "claude-sonnet-5")


def test_remap_selector_scopes_lanes_and_uses_the_target_minimum() -> None:
    main = table([(0, 0, 800, 0, 0, 10), (30, 800, 10, 0, 0, 10)], lane_key="m",
                 attribution=CC)
    sub = table([(0, 0, 800, 0, 0, 10)], lane_key="s", kind=LaneKind.SUBAGENT, attribution=CC)
    res = replay([main, sub], "model=claude-haiku-4-5@lane_kind:main")
    o = outcomes(res)
    assert not o[sub.requests[0].request_id].changed
    # Haiku caches from 4,096 tokens: the min-prefix gate uses the target's minimum, so the
    # 800-token prefix is no longer cached
    first = o[main.requests[0].request_id]
    assert first.changed
    assert (first.usage.cache_write_5m, first.usage.uncached_input) == (0, 800)


def test_remap_first_matching_clause_in_policy_order() -> None:
    lane = table([(0, 0, 10_000, 0, 0, 1_000)], attribution=CC)
    pol = Policy(name="x", model_remap=(("lane_kind:main", "claude-haiku-4-5"),
                                        ("all", "claude-sonnet-5")))
    res = replay(lane, pol)
    assert res.cost.nano == price(UsageBuckets(cache_write_5m=10_000, output=1_000),
                                  "claude-haiku-4-5")
    parsed = replay(lane, "model=claude-sonnet-5;model=claude-haiku-4-5@lane_kind:main")
    # the grammar orders repeated clauses by selector: "all" < "lane_kind:main"
    assert parsed.cost.nano == price(UsageBuckets(cache_write_5m=10_000, output=1_000),
                                     "claude-sonnet-5")


# ------------------------------------------------------------------------------------ effort


def _effort_lane(key: str, kind: LaneKind, effort: str | None, reasoning: bool = False) -> object:
    params = RequestParams(model_requested="claude-opus-5-5", effort=effort)
    rows = [(0, 0, 10_000, 0, 0, 1_000), (30, 10_000, 1_000, 0, 0, 1_000)]
    lane = table(rows, lane_key=key, kind=kind, attribution=CC, params=params)
    return lane


def test_effort_scaling_touches_only_matched_lanes_above_the_cap() -> None:
    main = _effort_lane("main", LaneKind.MAIN, "high")
    sub = _effort_lane("sub", LaneKind.SUBAGENT, "high")
    low = _effort_lane("low", LaneKind.MAIN, "medium")
    res = replay([main, sub, low], "effort=medium,scale=0.5@lane_kind:main")
    o = outcomes(res)
    assert not any(o[r.request_id].changed for r in sub.requests + low.requests)
    for req in main.requests:
        x = o[req.request_id]
        # th = floor(0.505 × 1000) = 505; cut = floor(505 × 0.5) = 252
        assert x.usage.output == 748
        assert x.changed
    # output $20/MTok: point saves 2 × 252 tokens; bounds use scale 0.25 / 0.75
    assert res.saving.nano == 2 * usd("0.00504")
    assert res.saving.high_nano == 2 * usd("0.00756")    # cut floor(505 × 0.75) = 378
    assert res.saving.low_nano == 2 * usd("0.00252")     # cut floor(505 × 0.25) = 126
    assert res.saving.upper_bound


def test_effort_uses_known_reasoning_tokens() -> None:
    params = RequestParams(model_requested="claude-opus-5-5", effort="max")
    from tokenbill.core.builders import make_lane, make_request

    from .helpers import at

    req = make_request("r", 0, at(0), UsageBuckets(cache_write_5m=10_000, output=1_000,
                                                   output_reasoning=400),
                       params=params, attribution=SDK)
    res = replay(make_lane([req]), "effort=high,scale=0.5")
    x = outcomes(res)[req.request_id]
    assert (x.usage.output, x.usage.output_reasoning) == (800, 200)


def test_effort_ranks_unknown_levels_as_unchanged() -> None:
    lane = _effort_lane("x", LaneKind.MAIN, "turbo")
    none = _effort_lane("y", LaneKind.MAIN, None)
    res = replay([lane, none], "effort=low,scale=0")
    assert res.saving.nano == 0 and not any(o.changed for o in res.outcomes or ())


# ------------------------------------------------------------------------------------ fast=off


def test_fast_off_flips_fast_toggle_misses_to_hits() -> None:
    from tokenbill.core.builders import make_lane, make_request

    from .helpers import at

    r0 = make_request("f", 0, at(0), UsageBuckets(cache_write_5m=100_000, output=500),
                      attribution=SDK)
    r1 = make_request("f", 1, at(30), UsageBuckets(cache_write_5m=102_000, output=500),
                      attribution=SDK, speed="fast")
    lane = make_lane([r0, r1])
    res = replay(lane, "fast=off")
    o = outcomes(res)
    assert not o[r0.request_id].changed
    x = o[r1.request_id]
    assert (x.usage.cache_read, x.usage.cache_write_5m) == (100_000, 2_000)
    observed = priced_request(r1).nano
    # fast Opus 5.5: $8 input → 5m writes at $10/M, output $40/M
    assert observed == usd("1.02") + usd("0.02")
    assert x.cost_nano == usd("0.02") + usd("0.01") + usd("0.01")
    assert res.saving.nano == observed - x.cost_nano


def test_fast_off_reprices_fast_hits_without_flips() -> None:
    lane = table([(0, 0, 100_000, 0, 0, 500), (30, 100_000, 2_000, 0, 0, 500)],
                 attribution=SDK, speed="fast")
    res = replay(lane, "fast=off")
    assert res.cost.nano == usd("0.50") + usd("0.02") + usd("0.01") + usd("0.02")
    assert res.cost.low_nano is None


# --------------------------------------------------------------------------- geo and regional


def test_geo_global_drops_the_us_premium() -> None:
    lane = table([(0, 0, 100_000, 0, 0, 500)], attribution=SDK, inference_geo="us")
    res = replay(lane, "geo=global")
    assert res.baseline.nano == usd("0.561")       # ($0.50 + $0.01) × 1.1
    assert res.cost.nano == usd("0.51")
    assert res.saving.nano == usd("0.051")
    assert any("exact rate arithmetic" in a for a in res.assumptions)


def test_regional_to_global_on_bedrock() -> None:
    lane = table([(0, 0, 100_000, 0, 0, 500)], model="claude-opus-5", attribution=SDK,
                 channel="bedrock", endpoint_scope="regional")
    res = replay(lane, "regional=global")
    assert res.baseline.nano == usd("0.6875") + usd("0.01375")   # ×1.1
    assert res.cost.nano == usd("0.625") + usd("0.0125")


def test_regional_to_global_resolves_an_unknown_scope_range() -> None:
    lane = table([(0, 0, 100_000, 0, 0, 500)], model="claude-opus-5", attribution=SDK,
                 channel="bedrock", endpoint_scope="unknown")
    res = replay(lane, "regional=global")
    assert res.baseline.low_nano is not None and res.baseline.evidence is Evidence.ESTIMATED
    assert res.cost.nano == usd("0.6375") and res.cost.low_nano is None
    assert res.saving.low_nano == 0 and res.saving.high_nano == usd("0.06375")


# -------------------------------------------------------------------------------------- batch


def _batch_lane(**kw: object) -> object:
    kw.setdefault("attribution", {"agent_product": "api", "workload_class": "ci"})
    return table([(0, 50_000, 10_000, 0, 1_000, 2_000)], **kw)


def test_batch_hit_band_point_low_high() -> None:
    res = replay(_batch_lane(), "batch=eligible")
    assert res.baseline.nano == usd("0.104")
    # point h = 0.64: reads 32,000, the rest 28,000 5m writes, all at the 0.5× batch tier
    assert res.cost.nano == usd("0.002") + usd("0.0032") + usd("0.07") + usd("0.02")
    assert res.cost.low_nano == usd("0.002") + usd("0.0049") + usd("0.0275") + usd("0.02")
    assert res.cost.high_nano == usd("0.002") + usd("0.0015") + usd("0.1125") + usd("0.02")
    assert res.saving.nano == usd("0.104") - res.cost.nano
    assert res.saving.low_nano == usd("0.104") - res.cost.high_nano
    x = next(iter(outcomes(res).values()))
    assert (x.usage.cache_read, x.usage.cache_write_5m) == (32_000, 28_000)


@pytest.mark.parametrize("kw", [
    {"attribution": {"agent_product": "api", "workload_class": "interactive"}},
    {"attribution": {"agent_product": "api", "workload_class": "ci",
                     "entrypoint": "claude-managed-agents"}},
    {"attribution": {"agent_product": "api", "workload_class": "ci"}, "speed": "fast"},
    {"attribution": {"agent_product": "api", "workload_class": "ci"}, "service_tier": "batch"},
])
def test_batch_predicate_excludes_ineligible_requests(kw: dict) -> None:
    res = replay(_batch_lane(**kw), "batch=eligible")
    assert same(res.cost, res.baseline) and res.saving.nano == 0


def test_batch_needs_a_single_request_lane() -> None:
    lane = table([(0, 0, 10_000, 0, 0, 100), (30, 10_000, 100, 0, 0, 100)],
                 attribution={"agent_product": "api", "workload_class": "eval"})
    assert replay(lane, "batch=eligible").saving.nano == 0


def test_batch_on_bedrock_has_no_caching() -> None:
    lane = _batch_lane(model="claude-opus-5", channel="bedrock", endpoint_scope="global")
    res = replay(lane, "batch=eligible")
    x = next(iter(outcomes(res).values()))
    assert (x.usage.cache_read, x.usage.cache_write, x.usage.uncached_input) == (0, 0, 61_000)
    # Bedrock Opus 5 at the 0.5× batch tier: $2.50 input, $12.50 output
    assert res.cost.nano == usd("0.1525") + usd("0.025")
    assert res.cost.low_nano is None


def test_batch_unsupported_model_is_skipped() -> None:
    lane = _batch_lane(model="claude-not-a-model")
    res = replay(lane, "batch=eligible")
    assert res.lanes_skipped == ((lane.lane_key, "batch not supported for model"),)

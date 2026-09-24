"""Hypothesis properties of the closed forms on random Claude-Code-shaped lanes billed by the
documented rules, and fuzzing of the generator's argument handling (only ``UsageError`` escapes).

Rates are Opus 5.5 at FakePricer list prices: uncached 4,000, read 200, 5m write 5,000, 1h write
8,000 and output 20,000 nano per token.
"""

from __future__ import annotations

from fractions import Fraction
from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core.builders import lane_from_table
from tokenbill.core.errors import UsageError
from tokenbill.core.records import Attribution, Lane
from tokenbill.synth import fleet as F
from tokenbill.synth import truth as T

BASE_S = 1_790_812_800
COSTER = T.Coster()

short_gap = st.integers(min_value=5, max_value=200)
long_gap = st.integers(min_value=320, max_value=3_500)
any_gap = st.one_of(short_gap, long_gap)


@st.composite
def lanes(draw: st.DrawFn, gap: st.SearchStrategy[int] = any_gap,
          n_max: int = 12) -> Lane:
    """A lane billed by the documented 5m rules: warm within 300 s, a full rewrite after."""
    n = draw(st.integers(min_value=1, max_value=n_max))
    t = BASE_S
    prefix = 0
    rows = []
    for i in range(n):
        uncached = draw(st.integers(min_value=0, max_value=9))
        out = draw(st.integers(min_value=0, max_value=3_000))
        if i == 0:
            reads, writes = 0, draw(st.integers(min_value=2_000, max_value=60_000))
        else:
            g = draw(gap)
            t += g
            appended = draw(st.integers(min_value=0, max_value=8_000))
            reads, writes = (prefix, appended) if g <= 300 else (0, prefix + appended)
        prefix = reads + writes
        rows.append((t, reads, writes, 0, uncached, out))
    key = draw(st.integers(min_value=0, max_value=10**6))
    return lane_from_table(rows, lane_key=f"L{key}",
                           attribution=Attribution(agent_product="agent_sdk"))


def _writes(lane: Lane) -> int:
    return sum(q.serving_inference.usage.cache_write for q in lane.requests)


@settings(max_examples=60, deadline=None)
@given(lanes(gap=short_gap))
def test_warm_lanes_only_pay_the_1h_write_premium(lane: Lane) -> None:
    assert T.ttl_1h_saving([lane], COSTER) == -3_000 * _writes(lane)
    assert T.keepalive_saving([lane], COSTER) == (0, 0)


@settings(max_examples=60, deadline=None)
@given(lanes(gap=long_gap))
def test_cold_lanes_flip_every_transition_under_1h(lane: Lane) -> None:
    expected = 0
    prev_prefix = None
    for req in lane.requests:
        u = req.serving_inference.usage
        observed = 4_000 * u.uncached_input + 5_000 * u.cache_write_5m + 20_000 * u.output
        if prev_prefix is None:
            policy = 4_000 * u.uncached_input + 8_000 * u.cache_write_5m + 20_000 * u.output
        else:
            reads = min(prev_prefix, u.cache_write_5m)   # E = P_{i−1}, all of it missed
            if reads < 2_000:                            # below the miss rule: no flip
                reads = 0
            policy = (4_000 * u.uncached_input + 200 * reads
                      + 8_000 * (u.cache_write_5m - reads) + 20_000 * u.output)
        expected += observed - policy
        prev_prefix = u.cache_read + u.cache_write
    assert T.ttl_1h_saving([lane], COSTER) == expected


@settings(max_examples=40, deadline=None)
@given(lanes(), lanes())
def test_closed_forms_add_over_disjoint_lanes(a: Lane, b: Lane) -> None:
    for form in (T.ttl_1h_saving, T.spend, T.restore_caching_saving):
        assert form([a, b], COSTER) == form([a], COSTER) + form([b], COSTER)
    ka, kb = T.keepalive_saving([a], COSTER), T.keepalive_saving([b], COSTER)
    assert T.keepalive_saving([a, b], COSTER) == (ka[0] + kb[0], ka[1] + kb[1])
    ta, tb = T.token_totals(a.requests), T.token_totals(b.requests)
    assert T.token_totals([*a.requests, *b.requests]) == ta + tb
    assert sum((ta + tb).as_tuple()) == sum((ta + tb).merged_writes().as_tuple())


@settings(max_examples=40, deadline=None)
@given(lanes())
def test_neutral_policies_change_nothing(lane: Lane) -> None:
    top = max(q.serving_inference.usage.total_input for q in lane.requests)
    assert T.compaction_window_saving([lane], COSTER, window=top, summary=20_283) == (0, 0)
    assert T.model_remap_saving([lane], COSTER, "claude-opus-5-5") == 0
    assert T.rate_premium([lane], COSTER, T.fast_to_standard) == 0
    assert T.effort_saving([lane], COSTER, max_level="medium", scale=Fraction(1)) == 0
    assert T.truncation([lane], COSTER) == (0, 0, 0, 0)
    assert T.edit_churn([lane], COSTER)[:2] == (0, 0)
    gaps = [b.ts_start_ms - a.ts_start_ms for a, b in pairwise(lane.requests)]
    _saving, pings = T.keepalive_saving([lane], COSTER)
    assert pings == sum(min(-(-g // 240_000) - 1, 15) for g in gaps if g > 240_000)


@settings(max_examples=80, deadline=None)
@given(seed=st.one_of(st.integers(), st.text(max_size=3), st.none(), st.floats()),
       devs=st.one_of(st.integers(min_value=-5, max_value=200), st.text(max_size=3),
                      st.booleans()),
       days=st.one_of(st.integers(min_value=-5, max_value=60), st.none(), st.floats()),
       scale=st.one_of(st.none(), st.integers(max_value=0), st.text(max_size=2)))
def test_argument_fuzz_only_raises_usage_error(seed: object, devs: object, days: object,
                                               scale: object) -> None:
    valid = (type(seed) is int and type(devs) is int and devs >= 61
             and type(days) is int and 7 <= days <= 31 and scale is None)
    try:
        F._check_args(seed, devs, days)
        if scale is not None:
            F.generate(seed=0, scale_requests=scale)  # type: ignore[arg-type]
    except UsageError:
        assert not valid
    else:
        assert valid

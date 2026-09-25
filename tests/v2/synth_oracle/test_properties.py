"""Property tests (hypothesis) of the oracle and the generators:

* identity (SPEC §9.1 #2): the observed policy returns ``cost == baseline`` to the nano, ranges
  included, with every outcome unchanged — on random lanes of every family;
* every outcome and figure has ``low ≤ point ≤ high``; ``saving = baseline − cost`` per request
  then summed (unchanged requests save exactly 0, changed ones with ranges crosswise);
  Σ outcomes = Σ per-lane = cost;
* shard additivity (§9.1 #6): replaying two disjoint halves and merging with
  ``core.shards.merge_replay`` equals one replay (families without cross-lane repairs);
* the small exact helpers and the generators' input handling.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.core.shards import merge_replay
from tokenbill.core.types import Policy
from tokenbill.synth.lanes_gen import FAMILIES, family_policies, random_lanes, rollout_panel
from tokenbill.synth.oracle import outcome_bounds, ping_count, round_half_even

from .helpers import bounds, replay

SETTINGS = settings(max_examples=12, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])
LOCAL_FAMILIES = [f for f in FAMILIES if f != "repairs"]   # no cross-lane repairs


@SETTINGS
@given(seed=st.integers(0, 10**6), family=st.sampled_from(FAMILIES))
def test_identity_on_random_lanes(seed: int, family: str) -> None:
    lanes = random_lanes(seed, 8, family=family)
    res = replay(lanes, Policy.observed())
    assert bounds(res.cost) == bounds(res.baseline)
    assert res.saving.nano == 0
    assert all(not o.changed for o in res.outcomes or ())
    assert len(res.outcomes or ()) == res.n_requests == sum(len(ln.requests) for ln in lanes)


@SETTINGS
@given(seed=st.integers(0, 10**6), family=st.sampled_from(FAMILIES), which=st.integers(1, 6))
def test_bounds_order_and_saving_arithmetic(seed: int, family: str, which: int) -> None:
    lanes = random_lanes(seed, 8, family=family)
    policies = family_policies(family)
    policy = policies[1 + which % (len(policies) - 1)]   # a non-observed policy
    res = replay(lanes, policy)
    observed = {o.request_id: outcome_bounds(o.cost_nano, o.low_nano, o.high_nano)
                for o in replay(lanes, Policy.observed()).outcomes or ()}
    total = 0
    per_request = [0, 0, 0]
    for o in res.outcomes or ():
        point, low, high = outcome_bounds(o.cost_nano, o.low_nano, o.high_nano)
        assert point is not None and low <= point <= high  # type: ignore[operator]
        total += point
        b_pt, b_lo, b_hi = observed[o.request_id]
        if not o.changed:
            assert o.extra == () and (point, low, high) == (b_pt, b_lo, b_hi)
            continue
        # saving per request, then summed; ranges crosswise (SPEC §3.5, §9.1 #1)
        per_request[0] += b_pt - point  # type: ignore[operator]
        per_request[1] += b_lo - high  # type: ignore[operator]
        per_request[2] += b_hi - low  # type: ignore[operator]
    assert total == res.cost.nano == sum(v for _k, v in res.per_lane)
    b, c, s = bounds(res.baseline), bounds(res.cost), bounds(res.saving)
    assert s == tuple(per_request)
    assert s[0] == b[0] - c[0]  # type: ignore[operator]
    # unaffected traffic never widens the saving: it is at most the aggregate crosswise range
    assert b[1] - c[2] <= s[1] and s[2] <= b[2] - c[1]  # type: ignore[operator]
    assert replay(lanes, policy).outcomes == res.outcomes   # deterministic


@SETTINGS
@given(seed=st.integers(0, 10**6), family=st.sampled_from(LOCAL_FAMILIES),
       which=st.integers(1, 6), cut=st.integers(1, 9))
def test_disjoint_halves_merge_to_the_whole(seed: int, family: str, which: int,
                                           cut: int) -> None:
    lanes = random_lanes(seed, 10, family=family)
    policies = family_policies(family)
    policy = policies[1 + which % (len(policies) - 1)]   # a non-observed policy
    whole = replay(lanes, policy)
    merged = merge_replay([replay(lanes[:cut], policy), replay(lanes[cut:], policy)])
    for name in ("baseline", "cost", "saving"):
        assert bounds(getattr(merged, name)) == bounds(getattr(whole, name)), name
    assert dict(merged.per_lane) == dict(whole.per_lane)
    assert (merged.added_calls, merged.keepalive_pings) == (whole.added_calls,
                                                            whole.keepalive_pings)


@given(num=st.integers(-10**12, 10**12), den=st.integers(1, 10**6))
def test_round_half_even_matches_decimal(num: int, den: int) -> None:
    expected = int((Decimal(num) / Decimal(den)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    if den <= 10**6 and abs(num) < 10**12:   # Decimal's 28 digits are exact here
        assert round_half_even(Fraction(num, den)) == expected


@given(gap=st.integers(0, 10**8), interval=st.integers(1, 3_600), max_idle=st.integers(0, 10**5))
def test_ping_count_formula(gap: int, interval: int, max_idle: int) -> None:
    n = ping_count(gap, interval, max_idle)
    step = interval * 1000
    assert 0 <= n <= max_idle // interval
    if gap <= step:
        assert n == 0
    else:
        uncapped = -(-gap // step) - 1
        assert n == min(uncapped, max_idle // interval)
        assert uncapped * step < gap <= (uncapped + 1) * step


@settings(max_examples=15, deadline=None)
@given(clusters=st.integers(2, 12), waves=st.integers(1, 4), extra_weeks=st.integers(1, 4),
       holdback=st.sampled_from(["0", "0.1", "0.25", "1"]), seed=st.integers(0, 1000))
def test_rollout_panel_shape(clusters: int, waves: int, extra_weeks: int, holdback: str,
                             seed: int) -> None:
    weeks = waves + extra_weeks
    try:
        rows = rollout_panel(clusters=clusters, weeks=weeks, true_effect="0.25", waves=waves,
                             holdback=holdback, seed=seed)
    except Exception as exc:   # only a holdback that leaves nothing treated may refuse
        from tokenbill.core.errors import UsageError

        assert isinstance(exc, UsageError)
        return
    assert len(rows) == clusters * 7 * weeks
    held = {r.cluster_id for r in rows if r.arm == "holdback"}
    assert all(not r.treated for r in rows if r.cluster_id in held)
    assert {r.wave for r in rows if r.arm == "treatment"} <= {str(k) for k in range(1, waves + 1)}


_TEXT = st.one_of(st.text(max_size=24), st.sampled_from(
    ["1e999999999", "-1e-999999999", "NaN", "Infinity", "0x10", "  0.25 ", "1_000", "", "∞"]))


@settings(max_examples=60, deadline=None)
@given(effect=st.one_of(_TEXT, st.floats(allow_nan=True, allow_infinity=True),
                        st.integers(-10**9, 10**9), st.decimals(allow_nan=True)),
       holdback=st.one_of(_TEXT, st.integers(-5, 30)), base=_TEXT,
       noise=st.integers(-10, 10**6))
def test_generator_inputs_raise_only_usage_errors(effect, holdback, base, noise) -> None:
    from tokenbill.core.errors import TokenbillError

    for call in (
        lambda: rollout_panel(clusters=4, weeks=3, true_effect=effect, waves=2,
                              holdback=holdback, seed=0),
        lambda: rollout_panel(clusters=3, weeks=2, true_effect="0.1", waves=1, holdback=0,
                              seed=0, base_usd_per_dev_day=base, noise_ppm=noise),
    ):
        try:
            call()
        except TokenbillError:
            pass


@settings(max_examples=40, deadline=None)
@given(name=st.text(max_size=12), n=st.integers(-3, 3))
def test_names_raise_only_usage_errors(name: str, n: int) -> None:
    from tokenbill.core.errors import UsageError
    from tokenbill.synth.lanes_gen import CLOSED_FORMS, closed_form

    for call in (lambda: closed_form(name), lambda: family_policies(name),
                 lambda: random_lanes(0, n, family=name if name else "ttl")):
        try:
            call()
        except UsageError:
            assert name not in CLOSED_FORMS or n < 0

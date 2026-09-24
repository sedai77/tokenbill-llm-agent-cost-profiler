"""Repairs (SPEC §9.3.6), each against a hand-computed fixture (Opus 5.5: input $4, output $20,
read $0.20, 5m write $5, 1h write $8 per MTok)."""

from __future__ import annotations

from tokenbill.core.builders import make_attempt, make_inference, make_lane, make_request
from tokenbill.core.records import InferenceKind, LaneKind, UsageBuckets

from .helpers import SDK, at, outcomes, priced_request, replay, table, usd

# --------------------------------------------------------------------------- restore_caching


def _no_cache_lane(gaps: tuple[int, ...] = (30, 30, 400, 30), **kw: object) -> object:
    ts = [0]
    for g in gaps:
        ts.append(ts[-1] + g)
    totals = [10_000, 11_000, 12_000, 13_000, 14_000]
    return table([(t, 0, 0, 0, total, 0) for t, total in zip(ts, totals, strict=False)],
                 attribution=SDK, **kw)


def test_restore_caching() -> None:
    lane = _no_cache_lane()
    res = replay(lane, "repair=restore_caching")
    o = outcomes(res)
    got = [(o[r.request_id].usage.cache_read, o[r.request_id].usage.cache_write_5m,
            o[r.request_id].usage.uncached_input) for r in lane.requests]
    assert got == [(0, 10_000, 0), (10_000, 1_000, 0), (11_000, 1_000, 0),
                   (0, 13_000, 0),                    # the 400 s gap exceeds τπ = 300 s
                   (13_000, 1_000, 0)]
    observed = usd("0.24")                               # 60,000 uncached × $4/M
    policy = usd("0.05") + usd("0.002") + usd("0.005") + usd("0.0022") + usd("0.005") + \
        usd("0.065") + usd("0.0026") + usd("0.005")
    assert res.baseline.nano == observed
    assert res.cost.nano == policy
    assert res.saving.nano == observed - policy


def test_restore_caching_uses_the_policy_ttl() -> None:
    lane = _no_cache_lane()
    res = replay(lane, "ttl=1h;repair=restore_caching")
    o = outcomes(res)
    fourth = o[lane.requests[3].request_id]
    assert (fourth.usage.cache_read, fourth.usage.cache_write_1h) == (12_000, 1_000)
    assert all(o[r.request_id].usage.cache_write_5m == 0 for r in lane.requests)


def test_restore_caching_eligibility() -> None:
    short = _no_cache_lane(gaps=(30, 30, 30))           # 4 requests
    assert replay(short, "repair=restore_caching").saving.nano == 0
    small = table([(i * 30, 0, 0, 0, 2_000, 0) for i in range(6)], attribution=SDK)
    assert replay(small, "repair=restore_caching").saving.nano == 0   # median T < 4096
    cached = table([(0, 0, 10_000, 0, 0, 0)] + [(i * 30, 10_000, 1_000, 0, 0, 0)
                                                for i in range(1, 6)], attribution=SDK)
    assert replay(cached, "repair=restore_caching").saving.nano == 0  # already caching


# --------------------------------------------------------------------------- stagger_fanout


def test_stagger_fanout() -> None:
    def sub(key: str, start: int, w: int, r: int = 0) -> object:
        return table([(start, r, w, 0, 0, 0)], lane_key=key, kind=LaneKind.SUBAGENT,
                     attribution=SDK)

    group = [sub("s1", 0, 20_000), sub("s2", 3, 22_000), sub("s3", 10, 21_000)]
    late = sub("s4", 25, 20_000)                        # 25 s after the group's first member
    warm = sub("s5", 4, 5_000, r=15_000)                # W < 0.8·T: not a cold fan-out member
    lanes = [*group, late, warm]
    res = replay(lanes, "repair=stagger_fanout")
    o = outcomes(res)
    first, second, third = (o[lane.requests[0].request_id] for lane in group)
    assert not first.changed
    assert (second.usage.cache_read, second.usage.cache_write_5m) == (20_000, 2_000)
    assert (third.usage.cache_read, third.usage.cache_write_5m) == (20_000, 1_000)
    assert not o[late.requests[0].request_id].changed
    assert not o[warm.requests[0].request_id].changed
    # each converted member saves 20,000 × ($5 − $0.20)/M
    assert res.saving.nano == 2 * usd("0.096")
    assert res.saving.upper_bound


def test_stagger_fanout_groups_stay_inside_a_cohort() -> None:
    a = table([(0, 0, 20_000, 0, 0, 0)], lane_key="a", kind=LaneKind.SUBAGENT,
              attribution={**SDK, "team": "t1"})
    b = table([(1, 0, 20_000, 0, 0, 0)], lane_key="b", kind=LaneKind.SUBAGENT,
              attribution={**SDK, "team": "t2"})
    assert replay([a, b], "repair=stagger_fanout").saving.nano == 0


# ------------------------------------------------------------------------ retry_backoff_cap


def _retry_request(n_failed: int, *, last_after_s: int = 400) -> object:
    """Request 1 of a lane: *n_failed* failed attempts (each billed a small uncertain partial),
    then the final attempt *last_after_s* after the first, cold (W5 = 102k)."""
    base = at(30)
    attempts = []
    for k in range(n_failed):
        partial = make_inference({"uncached_input": 1_000, "output": 10},
                                 inference_id=f"p{k}", billable=None)
        attempts.append(make_attempt([partial], ts_ms=base + k * 10_000, attempt_no=k,
                                     outcome="http_error", http_status=529))
    serving = make_inference({"cache_write_5m": 102_000, "output": 500}, inference_id="srv")
    attempts.append(make_attempt([serving], ts_ms=base + last_after_s * 1000,
                                 attempt_no=n_failed))
    return make_request("rt", 1, base, attempts=attempts, attribution=SDK)


def _retry_lane(n_failed: int, **kw: int) -> tuple[object, object]:
    r0 = make_request("rt", 0, at(0), UsageBuckets(cache_write_5m=100_000, output=500),
                      attribution=SDK)
    r1 = _retry_request(n_failed, **kw)
    return make_lane([r0, r1]), r1


def test_retry_backoff_cap_warms_the_final_attempt() -> None:
    lane, r1 = _retry_lane(1)
    res = replay(lane, "repair=retry_backoff_cap")
    x = outcomes(res)[r1.request_id]
    assert (x.usage.cache_read, x.usage.cache_write_5m) == (100_000, 2_000)
    # the serving attempt: $0.51 → $0.02 + $0.01 (output unchanged); the partial is kept
    assert res.saving.nano == usd("0.51") - usd("0.03")


def test_retry_backoff_cap_drops_attempts_beyond_three() -> None:
    lane, r1 = _retry_lane(4)
    res = replay(lane, "repair=retry_backoff_cap")
    x = outcomes(res)[r1.request_id]
    partial = usd("0.004") + usd("0.0002")               # 1,000 × $4 + 10 × $20 (per MTok)
    base = priced_request(r1)
    assert base.nano == 4 * partial + usd("0.51") + usd("0.01")
    # attempts 2 and 3 (0-based) are dropped; 0, 1 and the final attempt stay
    assert x.cost_nano == 2 * partial + usd("0.03") + usd("0.01")
    assert x.high_nano == x.cost_nano and x.low_nano == usd("0.03") + usd("0.01")


def test_retry_backoff_cap_needs_a_long_backoff() -> None:
    lane, _r1 = _retry_lane(1, last_after_s=100)          # within τ_obs = 300 s
    assert replay(lane, "repair=retry_backoff_cap").saving.nano == 0


# --------------------------------------------------------------------------- fallback_credit


def test_fallback_credit() -> None:
    r0 = make_request("fb", 0, at(0), UsageBuckets(cache_write_5m=50_000, output=100),
                      model="claude-fable-5", attribution=SDK)
    declined = make_inference({"uncached_input": 52_000, "output": 0}, model="claude-fable-5",
                              kind=InferenceKind.FALLBACK_DECLINED, billable=False,
                              inference_id="dec")
    fallback = make_inference({"cache_write_5m": 52_000, "output": 100}, model="claude-opus-4-8",
                              kind=InferenceKind.FALLBACK, inference_id="fbk")
    r1 = make_request("fb", 1, at(30), attempts=[make_attempt([declined, fallback],
                                                              ts_ms=at(30))], attribution=SDK)
    lane = make_lane([r0, r1])
    res = replay(lane, "repair=fallback_credit")
    x = outcomes(res)[r1.request_id]
    assert (x.usage.cache_read, x.usage.cache_write_5m) == (50_000, 2_000)
    # Opus 4.8: read $0.50, 5m write $6.25: 52,000 × 6.25 → 50,000 × 0.50 + 2,000 × 6.25
    assert res.saving.nano == usd("0.325") - usd("0.025") - usd("0.0125")


def test_fallback_credit_needs_a_large_rewrite() -> None:
    r0 = make_request("fb", 0, at(0), UsageBuckets(cache_write_5m=50_000, output=100),
                      model="claude-fable-5", attribution=SDK)
    fallback = make_inference({"cache_read": 20_000, "cache_write_5m": 32_000, "output": 100},
                              model="claude-opus-4-8", kind=InferenceKind.FALLBACK,
                              inference_id="fbk")
    r1 = make_request("fb", 1, at(30), attempts=[make_attempt([fallback], ts_ms=at(30))],
                      attribution=SDK)
    assert replay(make_lane([r0, r1]), "repair=fallback_credit").saving.nano == 0


# -------------------------------------------------------------------------- shared_ci_prefix


def _ci_run(key: str, start: int, w: int) -> object:
    return table([(start, 0, w, 0, 0, 0), (start + 20, w, 500, 0, 0, 0)], lane_key=key,
                 kind=LaneKind.API_RUN, attribution={**SDK, "workload_class": "ci"})


def test_shared_ci_prefix_without_a_floor() -> None:
    runs = [_ci_run("c1", 0, 30_000), _ci_run("c2", 200, 32_000), _ci_run("c3", 350, 31_000),
            _ci_run("c4", 2_000, 30_000)]                # c4 starts a new chain (1,650 s later)
    res = replay(runs, "repair=shared_ci_prefix")
    o = outcomes(res)
    firsts = [o[run.requests[0].request_id] for run in runs]
    assert not firsts[0].changed and not firsts[3].changed
    s_ci = 24_000                                        # floor(0.8 × min first-call W)
    assert (firsts[1].usage.cache_read, firsts[1].usage.cache_write_5m) == (s_ci, 8_000)
    assert (firsts[2].usage.cache_read, firsts[2].usage.cache_write_5m) == (s_ci, 7_000)
    assert res.saving.nano == 2 * (usd("0.12") - usd("0.0048"))
    assert res.saving.upper_bound
    assert not any(o[run.requests[1].request_id].changed for run in runs)


def test_shared_ci_prefix_reads_the_static_prefix_floor() -> None:
    runs = [_ci_run("c1", 0, 30_000), _ci_run("c2", 200, 32_000)]
    floor = {(runs[0].cache_scope_key, "claude-opus-5-5"): 20_000}
    res = replay(runs, "repair=shared_ci_prefix", floor=floor)
    second = outcomes(res)[runs[1].requests[0].request_id]
    assert (second.usage.cache_read, second.usage.cache_write_5m) == (20_000, 12_000)


def test_shared_ci_prefix_only_for_ci_lanes() -> None:
    runs = [table([(0, 0, 30_000, 0, 0, 0)], lane_key="i1", attribution=SDK),
            table([(100, 0, 30_000, 0, 0, 0)], lane_key="i2", attribution=SDK)]
    assert replay(runs, "repair=shared_ci_prefix").saving.nano == 0

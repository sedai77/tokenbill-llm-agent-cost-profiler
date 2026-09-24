"""Usage-level repairs (SPEC §9.3.6), calibrated mode (§9.4) and the result contract: billing
classes, unpriced usage, passthrough requests, block-level fields, determinism, helpers.
Opus 5.5 rates in nano per token: input 4,000, output 20,000, read 200, 5m 5,000, 1h 8,000."""

from __future__ import annotations

from fractions import Fraction

import pytest

from tokenbill.common import canonical_json
from tokenbill.core.builders import make_attempt, make_inference
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Calibration
from tokenbill.core.records import (
    InferenceKind,
    LaneKind,
    Outcome,
    UsageSource,
    WorkloadClass,
    to_json,
)
from tokenbill.core.types import CalibrationReport, Policy
from tokenbill.synth.lanes_gen import closed_form
from tokenbill.synth.oracle import (
    ReferenceReplay,
    outcome_bounds,
    ping_count,
    request_costs,
    round_half_even,
)

from .helpers import EPOCH_MS, PRICER, RULES, bounds, costs, lane, lane_of, replay, req

READ, W5, W1, OUT, IN = 200, 5_000, 8_000, 20_000, 4_000
CWD = "h_0123456789abcdef0123"


# ---------------------------------------------------------------------------------------------
# restore_caching
# ---------------------------------------------------------------------------------------------


def _nocache(uncached, gaps=(60, 60, 60, 60), key="Lnc"):
    rows, ts = [], 0
    for i, u in enumerate(uncached):
        if i:
            ts += gaps[i - 1]
        rows.append((ts, 0, 0, 0, u, 0))
    return lane(rows, lane_key=key, attribution={"agent_product": "agent_sdk"})


def test_restore_caching_rebuilds_a_warm_lane() -> None:
    ln = _nocache([10_000, 12_000, 14_000, 16_000, 18_000])
    res = replay([ln], "repair=restore_caching")
    assert [c[0] for c in costs(res)] == [50_000_000, 12_000_000, 12_400_000, 12_800_000,
                                          13_200_000]
    assert res.baseline.nano == 70_000 * IN
    usage = res.outcomes[1].usage  # type: ignore[index]
    assert (usage.cache_read, usage.cache_write_5m, usage.uncached_input) == (10_000, 2_000, 0)


def test_restore_caching_gaps_ambiguity_and_policy_ttl() -> None:
    cold = replay([_nocache([10_000, 12_000, 14_000, 16_000, 18_000], (60, 400, 60, 60))],
                  "repair=restore_caching")
    assert costs(cold)[2] == (14_000 * W5,) * 3
    edge = replay([_nocache([10_000, 12_000, 14_000, 16_000, 18_000], (60, 305, 60, 60))],
                  "repair=restore_caching")
    assert costs(edge)[2] == (14_000 * W5, 12_000 * READ + 2_000 * W5, 14_000 * W5)
    hour = replay([_nocache([10_000, 12_000, 14_000, 16_000, 18_000], (60, 400, 60, 60))],
                  "ttl=1h;repair=restore_caching")
    assert costs(hour)[2] == (12_000 * READ + 2_000 * W1,) * 3


@pytest.mark.parametrize("uncached", [
    [10_000, 12_000, 14_000, 16_000],            # fewer than 5 requests
    [1_000, 1_000, 1_000, 1_000, 30_000],         # median below 4,096
])
def test_restore_caching_predicate(uncached) -> None:
    assert replay([_nocache(uncached)], "repair=restore_caching").saving.nano == 0


def test_restore_caching_needs_a_lane_without_any_cache_activity() -> None:
    ln = lane([(0, 0, 0, 0, 10_000, 0), (60, 0, 0, 0, 12_000, 0), (120, 0, 0, 0, 14_000, 0),
               (180, 0, 0, 0, 16_000, 0), (240, 1_000, 0, 0, 17_000, 0)], lane_key="Lr")
    assert replay([ln], "repair=restore_caching").saving.nano == 0


# ---------------------------------------------------------------------------------------------
# stagger_fanout
# ---------------------------------------------------------------------------------------------


def _first(key, ts_ms, writes, cwd=CWD, reads=0):
    r = req(key, 0, 0, {"cache_read": reads, "cache_write_5m": writes},
            attribution={"agent_product": "claude_code", "cwd_key": cwd})
    from dataclasses import replace

    att = replace(r.attempts[0], ts_start_ms=EPOCH_MS + ts_ms)
    return lane_of([replace(r, attempts=(att,))], kind=LaneKind.SUBAGENT)


def test_stagger_fanout_shares_the_group_minimum() -> None:
    lanes = [_first("F1", 0, 50_000), _first("F2", 5_000, 60_000), _first("F3", 10_000, 40_000),
             _first("F4", 10_001, 45_000), _first("F5", 3_000, 30_000, cwd="h_" + "f" * 20),
             _first("F6", 2_000, 10_000, reads=20_000)]
    res = replay(lanes, "repair=stagger_fanout")
    per_lane = dict(res.per_lane)
    assert per_lane["F1"] == 50_000 * W5
    assert per_lane["F2"] == 40_000 * READ + 20_000 * W5
    assert per_lane["F3"] == 40_000 * READ
    assert per_lane["F4"] == 45_000 * W5              # 10.001 s after the group's first member
    assert per_lane["F5"] == 30_000 * W5              # another directory: its own group
    assert per_lane["F6"] == 20_000 * READ + 10_000 * W5   # warm first request: not a member
    assert res.saving.nano == 384_000_000 and res.saving.upper_bound


# ---------------------------------------------------------------------------------------------
# retry_backoff_cap and fallback_credit
# ---------------------------------------------------------------------------------------------


def test_retry_backoff_cap_warms_the_final_attempt_and_drops_extra_attempts() -> None:
    rid = "rq_retry"
    attempts = []
    for k in range(4):
        failed = make_inference({"uncached_input": 1_000, "output": 10},
                                inference_id=f"inf_failed_{k}",
                                usage_source=UsageSource.PARTIAL_STREAM, billable=None)
        attempts.append(make_attempt((failed,), ts_ms=EPOCH_MS + (30 + 100 * k) * 1000,
                                     attempt_no=k, attempt_id=f"at_{k}",
                                     outcome=Outcome.HTTP_ERROR, http_status=529))
    serving = make_inference({"cache_write_5m": 102_000}, inference_id="inf_serving")
    attempts.append(make_attempt((serving,), ts_ms=EPOCH_MS + 430_000, attempt_no=4,
                                 attempt_id="at_4"))
    retried = req("Lrt", 1, 30, None, request_id=rid, attempts=attempts,
                  attribution={"agent_product": "agent_sdk"})
    first = req("Lrt", 0, 0, {"cache_write_5m": 100_000},
                attribution={"agent_product": "agent_sdk"})
    ln = lane_of([first, retried], kind=LaneKind.API_RUN)
    failed_point = 1_000 * IN + 10 * OUT
    obs = request_costs(replay([ln], Policy.observed()))[rid]
    assert obs == (4 * failed_point + 102_000 * W5, 102_000 * W5,
                   4 * failed_point + 102_000 * W5)
    res = replay([ln], "repair=retry_backoff_cap")
    warm = 100_000 * READ + 2_000 * W5
    assert request_costs(res)[rid] == (2 * failed_point + warm, warm, 2 * failed_point + warm)


def _retried_warm(failed_at: int):
    """Request 1 already reads E = 100k and appends 60k (≥ 0.5·E) after 5 attempts spanning
    400 s; only attempt *failed_at* carries a (billable None) failed inference."""
    attempts = []
    for k in range(4):
        infs = ()
        if k == failed_at:
            infs = (make_inference({"uncached_input": 1_000, "output": 10},
                                   inference_id=f"inf_failed_{k}",
                                   usage_source=UsageSource.PARTIAL_STREAM, billable=None),)
        attempts.append(make_attempt(infs, ts_ms=EPOCH_MS + (30 + 100 * k) * 1000,
                                     attempt_no=k, attempt_id=f"at_{k}",
                                     outcome=Outcome.HTTP_ERROR, http_status=529))
    serving = make_inference({"cache_read": 100_000, "cache_write_5m": 60_000},
                             inference_id="inf_serving")
    attempts.append(make_attempt((serving,), ts_ms=EPOCH_MS + 430_000, attempt_no=4,
                                 attempt_id="at_4"))
    retried = req("Lrw", 1, 30, None, request_id="rq_warm", attempts=attempts,
                  attribution={"agent_product": "agent_sdk"})
    first = req("Lrw", 0, 0, {"cache_write_5m": 100_000},
                attribution={"agent_product": "agent_sdk"})
    return lane_of([first, retried], kind=LaneKind.API_RUN)


def test_retry_cap_dropping_attempts_without_billable_usage_is_no_change() -> None:
    # attempts 2 and 3 are dropped but bill nothing: usage and cost are identical, so the
    # request is unchanged and its failed-attempt range never enters the saving (§9.1 #1)
    kept = replay([_retried_warm(failed_at=0)], "repair=retry_backoff_cap")
    out = kept.outcomes[1]  # type: ignore[index]
    serving = 100_000 * READ + 60_000 * W5
    assert not out.changed
    assert request_costs(kept)["rq_warm"] == (serving + 1_000 * IN + 10 * OUT, serving,
                                              serving + 1_000 * IN + 10 * OUT)
    assert bounds(kept.saving) == (0, 0, 0) and kept.saving.low_nano is None
    # the same request whose dropped attempt carried the failed inference: a change, and the
    # dropped range enters the saving crosswise
    dropped = replay([_retried_warm(failed_at=2)], "repair=retry_backoff_cap")
    assert dropped.outcomes[1].changed  # type: ignore[index]
    assert request_costs(dropped)["rq_warm"] == (serving,) * 3
    assert bounds(dropped.saving) == (1_000 * IN + 10 * OUT, 0, 1_000 * IN + 10 * OUT)


def test_fallback_credit_reads_the_prefix_at_the_new_models_rate() -> None:
    declined = make_inference({"cache_read": 0, "output": 0}, model="claude-fable-5-1",
                              kind=InferenceKind.FALLBACK_DECLINED, inference_id="inf_dec",
                              billable=False, billing_rule_id="anthropic.refusal.pre_output")
    fallback = make_inference({"cache_write_5m": 102_000}, model="claude-opus-4-8",
                              kind=InferenceKind.FALLBACK, inference_id="inf_fb")
    att = make_attempt((declined, fallback), ts_ms=EPOCH_MS + 30_000, attempt_id="at_fb")
    refused = req("Lfb", 1, 30, None, request_id="rq_fb", attempts=[att])
    first = req("Lfb", 0, 0, {"cache_write_5m": 100_000})
    ln = lane_of([first, refused])
    res = replay([ln], "repair=fallback_credit")
    # opus 4.8: read 500, 5m write 6,250 nano per token
    assert request_costs(res)["rq_fb"] == (100_000 * 500 + 2_000 * 6_250,) * 3
    assert replay([ln], Policy.observed()).baseline.nano == 100_000 * W5 + 102_000 * 6_250


# ---------------------------------------------------------------------------------------------
# shared_ci_prefix
# ---------------------------------------------------------------------------------------------


def _ci_run(key, ts_s, writes, workload=WorkloadClass.CI):
    return lane([(ts_s, 0, writes, 0, 0, 0), (ts_s + 30, writes, 1_000, 0, 0, 0)],
                lane_key=key, scope="ws:ci",
                attribution={"agent_product": "claude_code", "workload_class": workload})


def test_shared_ci_prefix_reads_the_shared_prefix_on_consecutive_runs() -> None:
    lanes = [_ci_run("C1", 0, 30_000), _ci_run("C2", 120, 32_000), _ci_run("C3", 500, 31_000),
             _ci_run("C4", 600, 29_000), _ci_run("X1", 130, 30_000, WorkloadClass.INTERACTIVE)]
    res = replay(lanes, "repair=shared_ci_prefix")
    firsts = {ln.lane_key: request_costs(res)[ln.requests[0].request_id][0] for ln in lanes}
    s_ci = 23_200                                   # floor(0.8 · min first-call W = 29,000)
    assert firsts == {"C1": 30_000 * W5, "C2": s_ci * READ + 8_800 * W5, "C3": 31_000 * W5,
                      "C4": s_ci * READ + 5_800 * W5, "X1": 30_000 * W5}
    floor = replay(lanes, "repair=shared_ci_prefix", floor={("ws:ci", "claude-opus-5-5"): 20_000})
    c2 = floor.outcomes[2].usage  # type: ignore[index]
    assert (c2.cache_read, c2.cache_write_5m) == (20_000, 12_000)
    assert floor.saving.upper_bound


# ---------------------------------------------------------------------------------------------
# calibrated mode (§9.4)
# ---------------------------------------------------------------------------------------------


def _report(status="pass", mode_used="documented", rho=(("[300 s, 3600 s)", 30, 40, "0.6",
                                                         "0.87"),)):
    return CalibrationReport(
        granularity="day", n_periods=30, status=status, mode_used=mode_used, nmbe_pct="1",
        cvrmse_pct="5", nmbe_pct_calibrated="1", cvrmse_pct_calibrated="5",
        thresholds=("10", "30"), rho=rho, diag_confusion=(), diag_precision_recall=(),
        unlabeled=0, no_comparison_labels=0, ttl_corroboration=(0, 0), notes=())


def test_calibrated_mode_mixes_hit_and_no_hit_costs() -> None:
    lanes, _ = closed_form("A.1")
    res = replay(lanes, "ttl=1h", mode="calibrated", calibration=_report())
    assert res.mode == "calibrated" and res.calibration is Calibration.CALIBRATED
    assert [c[0] for c in costs(res)] == [810_000_000, 241_000_000, 245_300_000, 249_600_000]
    assert res.cost.calibration is Calibration.CALIBRATED


def test_calibrated_mode_pools_thin_bands_and_labels() -> None:
    lanes, _ = closed_form("A.1")
    thin = _report(rho=(("0-60s", 10, 10, "0.7", "1"), ("300-3600s", 5, 20, "0.1", "0.5")))
    res = replay(lanes, "ttl=1h", mode="calibrated", calibration=thin)
    assert costs(res)[1][0] == 436_000_000          # pooled ρ = 15/30
    empty = replay(lanes, "ttl=1h", mode="calibrated", calibration=_report(rho=()))
    assert costs(empty)[1][0] == 46_000_000          # no fitted band: the documented flip
    documented = replay(lanes, "ttl=1h", calibration=_report())
    assert documented.mode == "documented" and documented.calibration is Calibration.CALIBRATED
    failing = replay(lanes, "ttl=1h", mode="calibrated", calibration=_report(status="fail"))
    assert failing.mode == "documented" and failing.calibration is Calibration.UNCALIBRATED
    assert replay(lanes, "ttl=1h").calibration is Calibration.UNCALIBRATED


# ---------------------------------------------------------------------------------------------
# result contract
# ---------------------------------------------------------------------------------------------


def test_mixed_billing_classes_and_unknown_modes_raise() -> None:
    billed = lane([(0, 0, 1_000, 0, 0, 0)], lane_key="Lb")
    allowance = lane([(0, 0, 1_000, 0, 0, 0)], lane_key="La", billing_path="subscription")
    with pytest.raises(UsageError):
        replay([billed, allowance], "ttl=1h")
    with pytest.raises(UsageError):
        replay([billed], "ttl=1h", mode="fancy")


def test_allowance_lanes_are_list_equivalent() -> None:
    allowance = lane([(0, 0, 100_000, 0, 0, 0), (420, 0, 102_000, 0, 0, 0)], lane_key="La",
                     billing_path="subscription")
    res = replay([allowance], "ttl=1h")
    assert {res.baseline.basis, res.cost.basis, res.saving.basis} == {Basis.LIST_EQUIVALENT}
    assert res.saving.nano == 202_000 * W5 - (100_000 * W1 + 100_000 * READ + 2_000 * W1)


def test_unpriced_usage_is_never_zero() -> None:
    unknown = lane([(0, 0, 1_000, 0, 0, 0), (30, 1_000, 500, 0, 0, 0)], lane_key="Lu",
                   model="claude-unknown-9")
    priced = lane([(0, 0, 1_000, 0, 0, 0)], lane_key="Lp")
    obs = replay([unknown, priced], Policy.observed())
    assert obs.baseline.nano is None and obs.saving.nano is None
    res = replay([unknown, priced], "ttl=1h")
    assert res.cost.nano is None and res.saving.nano is None
    assert res.cost.note.startswith("unpriced:") and res.saving.note.startswith("unpriced:")
    assert dict(res.per_lane) == {"Lp": 1_000 * W1}
    assert request_costs(res)[unknown.requests[0].request_id] == (None, None, None)


def test_passthrough_only_requests_and_rate_transforms() -> None:
    residual = make_inference({"output": 1_000}, kind=InferenceKind.OUTPUT_RESIDUAL,
                              inference_id="inf_res", speed="fast")
    att = make_attempt((residual,), ts_ms=EPOCH_MS + 60_000, attempt_id="at_res")
    ln = lane_of([req("Lx", 0, 0, {"cache_write_5m": 10_000}),
                  req("Lx", 1, 60, None, request_id="rq_res", attempts=[att])])
    ttl = replay([ln], "ttl=1h")
    out = ttl.outcomes[1]  # type: ignore[index]
    assert not out.changed and out.usage.total_input == 0 and out.cost_nano == 1_000 * 40_000
    fast = replay([ln], "fast=off")
    assert request_costs(fast)["rq_res"][0] == 1_000 * OUT


def test_placeholder_output_keeps_its_range_under_policies() -> None:
    ln = lane_of([req("Lph", 0, 0, {"cache_write_5m": 10_000, "output": 5},
                      usage_source=UsageSource.MESSAGE_START_ONLY, output_upper=400)])
    res = replay([ln], "ttl=1h")
    assert costs(res)[0] == (10_000 * W1 + 5 * OUT, 10_000 * W1 + 5 * OUT,
                             10_000 * W1 + 400 * OUT)


def test_block_level_fields_are_ignored_with_an_assumption() -> None:
    ln = lane([(0, 0, 1_000, 0, 0, 0)], lane_key="Lbk")
    for spec in ("breakpoints=end", "repair=block:reorder-tools"):
        res = replay([ln], spec)
        assert res.saving.nano == 0
        assert any("block-level" in a for a in res.assumptions)


def test_counts_outcomes_and_determinism() -> None:
    empty = lane_of([], lane_key="Lempty")
    ln = lane([(0, 0, 1_000, 0, 0, 0), (30, 1_000, 500, 0, 0, 0)], lane_key="Ln")
    a = replay([ln, empty], "ttl=1h")
    b = ReferenceReplay().replay([empty, ln], Policy(name="x", ttl=(("all", "1h"),)),
                                 mode="documented", pricer=PRICER, rules=RULES,
                                 calibration=None, keep_outcomes=True)
    assert (a.n_lanes, a.n_requests) == (2, 2)
    assert dict(a.per_lane) == {"Ln": 1_000 * W1 + 1_000 * READ + 500 * W1, "Lempty": 0}
    assert canonical_json(to_json(a.outcomes[0])) == canonical_json(to_json(b.outcomes[0]))
    assert replay([ln], "ttl=1h", keep=False).outcomes is None
    assert replay([], "ttl=1h").cost.nano == 0


def test_module_helpers() -> None:
    assert [round_half_even(Fraction(n, 2)) for n in (1, 3, 5, -1, -3)] == [0, 2, 2, 0, -2]
    assert round_half_even(Fraction(7, 3)) == 2 and round_half_even(Fraction(8, 3)) == 3
    assert [ping_count(g, 240, 3600) for g in (0, 240_000, 240_001, 420_000, 1_200_000,
                                               7_200_000)] == [0, 0, 1, 1, 4, 15]
    assert outcome_bounds(None, 1, 2) == (None, None, None)
    assert outcome_bounds(5, None, None) == (5, 5, 5)
    with pytest.raises(UsageError):
        request_costs(replay([lane([(0, 0, 1, 0, 0, 0)])], "ttl=1h", keep=False))


def test_retry_cap_ignores_first_requests_and_cold_lanes() -> None:
    first_only = lane_of([req("Lf", 0, 0, {"cache_write_5m": 1_000})], kind=LaneKind.API_RUN)
    assert replay([first_only], "repair=retry_backoff_cap").saving.nano == 0
    assert bounds(replay([first_only], "repair=fallback_credit").cost)[0] == 1_000 * W5


@pytest.mark.parametrize("change", [{"client_version": ("2.1.250", "2.1.270")},
                                    {"cwd_key": (CWD, "h_" + "a" * 20)}])
def test_client_upgrade_and_directory_change_block_a_ttl_flip(change) -> None:
    (key, (before, after)), = change.items()
    reqs = [req("Lcu", 0, 0, {"cache_write_5m": 100_000},
                attribution={"agent_product": "claude_code", key: before}),
            req("Lcu", 1, 420, {"cache_write_5m": 102_000},
                attribution={"agent_product": "claude_code", key: after})]
    res = replay([lane_of(reqs)], "ttl=1h")
    assert res.outcomes is not None and res.outcomes[1].usage.cache_read == 0


def test_a_non_billable_serving_inference_keeps_the_chain_but_costs_nothing() -> None:
    errored = req("Lnb", 1, 30, {"cache_read": 100_000, "cache_write_5m": 2_000},
                  billable=False, billing_rule_id="anthropic.batch.errored_canceled_expired")
    ln = lane_of([req("Lnb", 0, 0, {"cache_write_5m": 100_000}), errored,
                  req("Lnb", 2, 500, {"cache_write_5m": 104_000})])
    res = replay([ln], "ttl=1h")
    assert costs(res)[1] == (0, 0, 0)
    # request 2 reads the non-billed request's prefix (P' = 102k) after the 470 s gap
    assert res.outcomes[2].usage.cache_read == 102_000  # type: ignore[index]


def test_write_class_helpers() -> None:
    from tokenbill.core.records import UsageBuckets
    from tokenbill.synth.oracle import _own_class, _own_ttl_s

    other = UsageBuckets(cache_write_other=10, cache_write_other_ttl_s=1_800)
    assert _own_ttl_s(other, None) == 1_800 and _own_class(other) == ("other", 1_800)
    unknown = UsageBuckets(cache_write_unknown=10)
    assert _own_ttl_s(unknown, "1h") == 3_600 and _own_ttl_s(unknown, None) is None
    assert _own_class(UsageBuckets(cache_write_5m=1, cache_write_1h=1)) is None
    assert _own_ttl_s(UsageBuckets(cache_write_5m=1, cache_write_1h=1), None) == 300


def test_stagger_ignores_lanes_without_a_serving_request() -> None:
    residual = make_inference({"output": 10}, kind=InferenceKind.OUTPUT_RESIDUAL,
                              inference_id="inf_only_residual")
    att = make_attempt((residual,), ts_ms=EPOCH_MS, attempt_id="at_only_residual")
    only = lane_of([req("Lonly", 0, 0, None, request_id="rq_only", attempts=[att])],
                   kind=LaneKind.SUBAGENT)
    res = replay([only, _first("F1", 0, 50_000), _first("F2", 1_000, 50_000)],
                 "repair=stagger_fanout")
    assert res.saving.nano == 50_000 * (W5 - READ)

"""The model gate (SPEC §9.6, D7): predictive calibration, ρ, cross-validation, diagnostics."""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from tokenbill.core.builders import make_lane, make_request
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Calibration
from tokenbill.core.records import (
    CacheDiagnostic,
    LaneEvent,
    RequestParams,
    UsageBuckets,
    to_json,
)
from tokenbill.core.types import CalibrationPartial
from tokenbill.sim.calibrate import (
    _undiagnosed_cause,
    _wilson,
    calibrate,
    calibrate_lanes,
    calibrate_pass1,
    calibrate_pass2,
    finish_calibration,
    fit_rho_by_fold,
    merge_partials,
)
from tokenbill.sim.usage_replay import GAP_BANDS

from .helpers import DAY_S, PRICER, RULES, SDK, at, table

_HIT_GAPS = (20, 45, 90, 150, 240)
_MISS_GAPS = (420, 900, 1_800)


def documented_lanes(days: int, *, flip: float = 0.0, seed: int = 0, per_day: int = 12,
                     n: int = 30, warm_misses: float = 0.0) -> list:
    """Lanes billed exactly as the documented rules predict (5m writes, τ = 300 s, S = 0):
    predicted hits read ``E = T_{i−1}``, predicted misses rewrite everything. With *flip*, that
    share of predicted hits (seeded) is billed as a miss instead; with *warm_misses*, that share
    of predicted misses is billed as a hit (as if the cache lived longer than documented)."""
    rnd = random.Random(seed)
    lanes = []
    for d in range(days):
        for k in range(per_day):
            t = d * DAY_S + k * 600
            total = rnd.randint(40_000, 80_000)
            rows = [(t, 0, total, 0, 0, rnd.randint(100, 900))]
            for _ in range(1, n):
                gap = rnd.choice(_HIT_GAPS * 3 + _MISS_GAPS)
                t += gap
                new = total + rnd.randint(1_000, 6_000)
                out = rnd.randint(100, 900)
                warm = gap <= 300 and rnd.random() >= flip
                if gap > 300 and rnd.random() < warm_misses:
                    warm = True
                if warm:
                    rows.append((t, total, new - total, 0, 0, out))
                else:
                    rows.append((t, 0, new, 0, 0, out))
                total = new
            lanes.append(table(rows, lane_key=f"D{d:02d}-{k:02d}", attribution=SDK))
    return lanes


def _cal(lanes: list, **kw: object) -> object:
    return calibrate_lanes(lanes, pricer=PRICER, rules=RULES, **kw)


# ------------------------------------------------------------------------ the cost comparison


def test_documented_lanes_pass_with_zero_error() -> None:
    report = _cal(documented_lanes(14))
    assert report.status == "pass" and report.mode_used == "documented"
    assert report.nmbe_pct == "0" and report.cvrmse_pct == "0"
    assert report.n_periods == 14 and report.granularity == "day"
    assert report.thresholds == ("10", "30")
    assert report.calibration() is Calibration.CALIBRATED
    by_band = {band: (h, t) for band, h, t, _lo, _hi in report.rho}
    assert [band for band, *_ in report.rho] == [b for b, _lo, _hi in GAP_BANDS]
    assert by_band["0s-60s"][0] == by_band["0s-60s"][1] > 30
    assert by_band["300s-3600s"] == (0, 0)          # predicted misses are not ρ trials


def test_ten_percent_flipped_hits_fit_rho_and_pass_calibrated() -> None:
    report = _cal(documented_lanes(14, flip=0.10, seed=7))
    assert report.status == "pass" and report.mode_used == "calibrated"
    assert abs(Decimal(report.nmbe_pct)) > 10                   # documented fails
    assert abs(Decimal(report.nmbe_pct_calibrated)) <= 10
    assert Decimal(report.cvrmse_pct_calibrated) <= 30
    hits = sum(h for _b, h, _t, _lo, _hi in report.rho)
    trials = sum(t for _b, _h, t, _lo, _hi in report.rho)
    assert abs(Decimal(hits) / Decimal(trials) - Decimal("0.90")) <= Decimal("0.03")
    for _band, h, t, lo, hi in report.rho:
        if t:
            assert Decimal(lo) <= Decimal(h) / Decimal(t) <= Decimal(hi)
    assert report.calibration() is Calibration.CALIBRATED


def test_heavy_disagreement_fails() -> None:
    # the cache outlives the documented TTL: ρ cannot fix predicted misses billed as hits
    report = _cal(documented_lanes(13, warm_misses=0.8, seed=3))
    assert report.status == "fail" and report.mode_used is None
    assert report.calibration() is Calibration.UNCALIBRATED


def test_eleven_periods_are_insufficient() -> None:
    report = _cal(documented_lanes(11))
    assert report.status == "insufficient_data" and report.mode_used is None
    assert report.n_periods == 11
    assert report.nmbe_pct == "0"                                # still reported
    assert any("11 periods < 12" in n for n in report.notes)
    assert report.calibration() is Calibration.UNCALIBRATED


def test_month_granularity_uses_the_monthly_thresholds() -> None:
    lanes = []
    for m in range(12):
        batch = documented_lanes(1, seed=m, per_day=2, n=6)
        lanes.extend(table([(r.ts_start_ms // 1000 - at(0) // 1000 + m * 31 * DAY_S,
                             r.attempts[0].inferences[0].usage.cache_read,
                             r.attempts[0].inferences[0].usage.cache_write_5m, 0, 0,
                             r.attempts[0].inferences[0].usage.output)
                            for r in lane.requests], lane_key=f"M{m:02d}{lane.lane_key}",
                           attribution=SDK) for lane in batch)
    report = _cal(lanes, granularity="month")
    assert report.granularity == "month" and report.thresholds == ("5", "15")
    assert report.n_periods == 12 and report.status == "pass"


def test_zero_billed_cost_is_insufficient() -> None:
    from tokenbill.core.builders import FlatRates

    class Free(FlatRates):
        INPUT = Decimal(0)
        OUTPUT = Decimal(0)
        WEB_SEARCH_USD = Decimal(0)

    lanes = [table([(d * DAY_S, 0, 50_000, 0, 0, 0), (d * DAY_S + 30, 50_000, 1_000, 0, 0, 0)],
                   lane_key=f"z{d}", attribution=SDK) for d in range(12)]
    report = calibrate_lanes(lanes, pricer=Free(), rules=RULES)
    assert report.n_periods == 12
    assert report.status == "insufficient_data"
    assert any("no billed cost" in n for n in report.notes)


def test_predictions_never_read_the_transitions_own_reads() -> None:
    hit = table([(0, 0, 50_000, 0, 0, 10), (30, 50_000, 2_000, 0, 0, 10),
                 (60, 52_000, 2_000, 0, 0, 10)], lane_key="r", attribution=SDK)
    missed = table([(0, 0, 50_000, 0, 0, 10), (30, 50_000, 2_000, 0, 0, 10),
                    (60, 0, 54_000, 0, 0, 10)], lane_key="r", attribution=SDK)
    p_hit = calibrate_pass1([hit], pricer=PRICER, rules=RULES)
    p_missed = calibrate_pass1([missed], pricer=PRICER, rules=RULES)
    assert p_hit.period_documented == p_missed.period_documented
    assert p_hit.period_billed != p_missed.period_billed
    assert p_hit.rho_counts == ((0, "0s-60s", 2, 2),) or p_hit.rho_counts[0][2:] == (2, 2)
    assert p_missed.rho_counts[0][2:] == (1, 2)


def test_one_batch_equals_five_batches() -> None:
    lanes = documented_lanes(14, flip=0.1, seed=11)
    one = calibrate(lambda: [lanes], pricer=PRICER, rules=RULES)
    five = calibrate(lambda: [lanes[i::5] for i in range(5)], pricer=PRICER, rules=RULES)
    assert to_json(one) == to_json(five)
    parts = [calibrate_pass1(lanes[i::3], pricer=PRICER, rules=RULES) for i in range(3)]
    assert merge_partials(parts) == merge_partials(list(reversed(parts)))


def test_out_of_fold_rho() -> None:
    parts = [CalibrationPartial(
        granularity="day", period_billed=(), period_documented=(), period_calibrated=(),
        rho_counts=((0, "0s-60s", 40, 40), (1, "0s-60s", 20, 40), (1, "60s-300s", 5, 10)),
        confusion=(), ttl_corroboration=(0, 0), unlabeled=0, no_comparison_labels=0)]
    rho = fit_rho_by_fold(parts, folds=2)
    assert rho[0]["0s-60s"] == Decimal("0.5")                # fitted on fold 1 only
    assert rho[0]["60s-300s"] == Decimal(25) / Decimal(50)   # 10 trials < 30: pooled of fold 1
    assert rho[1]["0s-60s"] == Decimal(1)
    assert rho[1]["3600s+"] == Decimal(1)                    # pooled of fold 0
    assert fit_rho_by_fold([], folds=3) == {k: {b: Decimal(1) for b, _l, _h in GAP_BANDS}
                                            for k in range(3)}


def test_pass2_blends_predicted_hits() -> None:
    lanes = documented_lanes(1, per_day=2, n=5, seed=2)
    kw = {"pricer": PRICER, "rules": RULES}
    ones = {k: {b: Decimal(1) for b, _l, _h in GAP_BANDS} for k in range(5)}
    zeros = {k: {b: Decimal(0) for b, _l, _h in GAP_BANDS} for k in range(5)}
    p1 = calibrate_pass1(lanes, **kw)
    assert calibrate_pass2(lanes, ones, **kw).period_calibrated == p1.period_documented
    all_miss = calibrate_pass2(lanes, zeros, **kw).period_calibrated
    assert dict(all_miss)[p1.period_billed[0][0]] > dict(p1.period_documented)[
        p1.period_billed[0][0]]


def test_wilson_interval() -> None:
    assert _wilson(90, 100) == ("0.8256", "0.9448")
    assert _wilson(0, 0) == ("0", "1")
    lo, hi = _wilson(30, 30)
    assert hi == "1" and Decimal(lo) > Decimal("0.88")


def test_small_bands_report_the_pooled_interval() -> None:
    lanes = documented_lanes(12, per_day=4, n=8, seed=5)
    report = _cal(lanes)
    pooled_h = sum(h for _b, h, _t, _lo, _hi in report.rho)
    pooled_t = sum(t for _b, _h, t, _lo, _hi in report.rho)
    for _band, _h, t, lo, hi in report.rho:
        if t < 30:
            assert (lo, hi) == _wilson(pooled_h, pooled_t)


# ------------------------------------------------------------------------------- diagnostics


def _diag(reason: str, source: str = "anthropic.cache_diagnostics") -> CacheDiagnostic:
    return CacheDiagnostic(reason=reason, provider_reason=reason,
                           missed_input_tokens_estimate=None, source=source)


def _labeled_pair(key: str, reason: str, *, gap_s: int = 30, model2: str = "claude-opus-5-5",
                  day: int = 0, events: tuple = (), second: UsageBuckets | None = None) -> object:
    r0 = make_request(key, 0, at(day * DAY_S), UsageBuckets(cache_write_5m=50_000, output=10),
                      attribution=SDK)
    r1 = make_request(key, 1, at(day * DAY_S + gap_s),
                      second or UsageBuckets(cache_write_5m=52_000, output=10), model2,
                      attribution=SDK, diagnostics=_diag(reason))
    return make_lane([r0, r1], lane_key=key, events=list(events))


def test_ttl_corroboration_128_of_128() -> None:
    lanes = [_labeled_pair(f"idle{i}", "previous_message_not_found", gap_s=3_700 + i,
                           day=i % 12) for i in range(128)]
    report = _cal(lanes)
    assert report.ttl_corroboration == (128, 128)
    assert report.no_comparison_labels == 128
    assert report.diag_precision_recall == ()
    assert report.diag_confusion == (("ttl-expiry", "previous_message_not_found", 128),)
    assert report.unlabeled == 0


def test_model_changed_label_vs_predicted_model_switch() -> None:
    lanes = [_labeled_pair(f"m{i}", "model_changed", model2="claude-sonnet-5") for i in range(3)]
    lanes.append(_labeled_pair("h", "model_changed"))       # labeled, but billed as a miss
    report = _cal(lanes)
    assert ("model-switch", "model_changed", 3) in report.diag_confusion
    assert dict((c, (p, r)) for c, p, r in report.diag_precision_recall)["model_changed"] == \
        ("1", "0.75")


def _openai_pair(key: str) -> object:
    usage0 = UsageBuckets(cache_write_other=50_000, cache_write_other_ttl_s=1800, output=10)
    usage1 = UsageBuckets(cache_write_other=52_000, cache_write_other_ttl_s=1800, output=10)
    r0 = make_request(key, 0, at(0), usage0, "gpt-5.6-sol", attribution=SDK,
                      params=RequestParams(model_requested="gpt-5.6-sol", effort="high"))
    r1 = make_request(key, 1, at(60), usage1, "gpt-5.6-sol", attribution=SDK,
                      params=RequestParams(model_requested="gpt-5.6-sol", effort="low"),
                      diagnostics=_diag("param_changed", "openai.prompt_cache_diagnostics"))
    return make_lane([r0, r1], lane_key=key)


def test_openai_param_changed_vs_predicted_effort_change() -> None:
    report = _cal([_openai_pair("o1"), _openai_pair("o2")])
    assert report.diag_confusion == (("param-change", "param_changed", 2),)
    assert report.diag_precision_recall == (("param_changed", "1", "1"),)


def test_per_provider_precision_recall_when_both_are_present() -> None:
    lanes = [_openai_pair("o1"), _labeled_pair("a1", "model_changed", model2="claude-sonnet-5")]
    report = _cal(lanes)
    assert report.diag_precision_recall == (("anthropic:model_changed", "1", "1"),
                                            ("openai:param_changed", "1", "1"))
    assert any("per provider" in n for n in report.notes)


def test_unavailable_counts_as_no_comparison() -> None:
    report = _cal([_labeled_pair("u", "unavailable"), _labeled_pair("v", "unavailable")])
    assert report.no_comparison_labels == 2
    assert report.diag_precision_recall == ()
    assert report.ttl_corroboration == (0, 0)


def test_expected_rebuilds_and_key_changes_are_not_scored() -> None:
    compaction = LaneEvent(lane_key="c", ts_ms=at(10), kind="compaction")
    lanes = [_labeled_pair("c", "messages_changed", events=(compaction,)),
             _labeled_pair("k", "key_changed"),
             _labeled_pair("x", "compacted"),
             _labeled_pair("t", "tools_changed")]
    report = _cal(lanes)
    confusion = {(c, r): n for c, r, n in report.diag_confusion}
    assert confusion[("compaction", "messages_changed")] == 1
    assert confusion[("unexplained", "key_changed")] == 1
    assert confusion[("unexplained", "compacted")] == 1       # rule 5 undone: no reset
    assert confusion[("unexplained", "tools_changed")] == 1
    assert report.diag_precision_recall == (("tools_changed", "n/a", "0"),)
    assert any("key_changed" in n for n in report.notes)


def test_undiagnosed_cause_rule_five_is_undone() -> None:
    from tokenbill.core.types import Transition

    def t(cause: str, sub: str | None, reason: str, miss: bool = True, total: int = 90) -> object:
        return Transition(request_id="r", lane_key="l", index=1, gap_ms=1, total=total,
                          reads=0, prev_prefix=100, expected_reuse=90, missed=90,
                          is_miss_event=miss, cause=cause, sub_cause=sub, ttl_s=300,
                          ambiguous=False, predicted_hit=True, diag_reason=reason)

    assert _undiagnosed_cause(t("hit", None, "unavailable", miss=False), False, 100) == "hit"
    assert _undiagnosed_cause(t("tools-changed", None, "tools_changed"), False, 100) == \
        "unexplained"
    assert _undiagnosed_cause(t("tools-changed", None, "tools_changed", total=50), False,
                              100) == "context-shrank"
    assert _undiagnosed_cause(t("compaction", None, "compacted"), True, 100) == "compaction"
    assert _undiagnosed_cause(t("model-switch", "user", "model_changed"), False, 100) == \
        "model-switch"


def test_unlabeled_transitions_are_counted() -> None:
    report = _cal(documented_lanes(1, per_day=1, n=5))
    assert report.unlabeled == 4 and report.diag_confusion == ()


# ----------------------------------------------------------------------------------- guards


def test_argument_validation() -> None:
    lanes = documented_lanes(1, per_day=1, n=3)
    with pytest.raises(UsageError):
        _cal(lanes, granularity="hour")
    with pytest.raises(UsageError):
        _cal(lanes, folds=1)
    with pytest.raises(UsageError):
        calibrate(lanes, pricer=PRICER, rules=RULES)  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        calibrate_pass1(["not a lane"], pricer=PRICER, rules=RULES)  # type: ignore[list-item]
    with pytest.raises(UsageError):
        merge_partials([])
    day = calibrate_pass1(lanes, pricer=PRICER, rules=RULES)
    month = calibrate_pass1(lanes, pricer=PRICER, rules=RULES, granularity="month")
    with pytest.raises(UsageError):
        merge_partials([day, month])
    with pytest.raises(UsageError):
        finish_calibration([month], [], granularity="day", folds=5)
    with pytest.raises(UsageError):
        fit_rho_by_fold([day], folds=0)


def test_rules_default_and_unpriced_transitions_are_skipped() -> None:
    priced = documented_lanes(12, per_day=1, n=4)
    unpriced = [table([(0, 0, 50_000, 0, 0, 10), (30, 50_000, 1_000, 0, 0, 10)],
                      model="claude-not-a-model", lane_key="u", attribution=SDK)]
    a = calibrate_lanes(priced, pricer=PRICER, rules=None)
    b = calibrate_lanes(priced + unpriced, pricer=PRICER, rules=None)
    assert a.nmbe_pct == b.nmbe_pct and a.n_periods == b.n_periods


def test_report_has_no_floats() -> None:
    report = _cal(documented_lanes(12, flip=0.1, per_day=3, n=10))

    def leaves(obj: object) -> list:
        if isinstance(obj, dict):
            return [x for v in obj.values() for x in leaves(v)]
        if isinstance(obj, list):
            return [x for v in obj for x in leaves(v)]
        return [obj]

    assert not any(isinstance(v, float) for v in leaves(to_json(report)))
